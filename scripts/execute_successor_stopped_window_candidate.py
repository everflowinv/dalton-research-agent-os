#!/usr/bin/env python3
"""Execute a reviewed successor release using the proven R11a rollback shell.

The template is inert until a frozen source, wheel, full suite, copied-state
rehearsal, exact current snapshots and an accepted manifest are supplied.  The
R11a helper retains ownership of stop/drain/fresh-backup/restart/rollback.  This
module adds only the successor wheel and the five-file configuration CAS.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from scripts import execute_r11a_stopped_window_candidate as r11
from scripts import prepare_release_acceptance_candidate as release_acceptance
from scripts.prepare_successor_config_transition import (
    DOCUMENT_CONFIG, LANE_CONFIG, apply_transition, expected_transition_state,
)


SCHEMA_VERSION = "successor-stopped-window-candidate-0.1"
HEX40 = re.compile(r"[0-9a-f]{40}")
HEX64 = re.compile(r"[0-9a-f]{64}")
ARTIFACT_NAMES = frozenset({
    "wheel", "full_suite_receipt", "full_suite_log", "full_suite_runner",
    "full_suite_native_result", "wheel_verification",
    "copied_state_rehearsal_binding", "transition_manifest",
    "model_config_before_snapshot", "model_config_after_snapshot",
    "service_config_snapshot", "openclaw_config_snapshot",
    "mission_authority_snapshot", "provider_plugin_snapshot",
    "web_v6_activation_receipt", "alpha_v3_activation_receipt",
})
MANIFEST_FIELDS = frozenset({
    "schema_version", "release_ref", "status", "acceptance_state",
    "deployment_state", "source", "artifacts", "acceptance", "runtime",
    "health_acceptance", "boundaries", "content_hash",
})


class SuccessorExecuteError(RuntimeError):
    pass


def need(ok: Any, reason: str) -> None:
    if not ok:
        raise SuccessorExecuteError(reason)


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_hash(value: Any) -> str:
    return hashlib.sha256((json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n").encode()).hexdigest()


def template() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION, "release_ref": None,
        "status": "inputs_incomplete", "acceptance_state": "pending",
        "deployment_state": "not_started",
        "source": {"root": None, "commit": None},
        "artifacts": {name: {"file": None, "sha256": None}
                      for name in sorted(ARTIFACT_NAMES)},
        "acceptance": {
            "state": "pending", "full_suite_receipt_sha256": None,
            "wheel_sha256": None,
            "copied_state_rehearsal_binding_sha256": None,
            "health_acceptance_required": True,
        },
        "runtime": {
            "model_config_count_before": None,
            "model_config_count_after": None,
            "backup_keep_latest": 3, "thesis_impact_enabled": False,
        },
        "health_acceptance": {
            "sample_count": None, "interval_seconds": None,
            "minimum_duration_seconds": None,
        },
        "boundaries": {"manifest_publication": False, "model_calls": False},
    }


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SuccessorExecuteError(f"invalid JSON: {path}") from exc
    need(isinstance(value, dict), f"JSON artifact is not an object: {path}")
    return value


def packet_preflight(packet: Path) -> tuple[dict[str, Any], dict[str, Path]]:
    manifest_path = packet / "release-manifest.candidate.json"
    need(packet.is_dir() and not packet.is_symlink()
         and manifest_path.is_file() and not manifest_path.is_symlink(),
         "successor candidate packet is unavailable")
    manifest = load_json(manifest_path)
    unsigned = dict(manifest); asserted = unsigned.pop("content_hash", None)
    need(set(manifest) == MANIFEST_FIELDS
         and manifest.get("schema_version") == SCHEMA_VERSION
         and isinstance(manifest.get("release_ref"), str)
         and bool(manifest["release_ref"].strip())
         and manifest.get("status") == "accepted_for_stopped_window"
         and manifest.get("acceptance_state") == "accepted"
         and manifest.get("deployment_state") == "not_started"
         and asserted == canonical_hash(unsigned),
         "successor candidate manifest differs")
    source = manifest.get("source", {})
    need(isinstance(source, Mapping) and set(source) == {"root", "commit"}
         and isinstance(source.get("root"), str)
         and Path(source["root"]).is_absolute()
         and HEX40.fullmatch(str(source.get("commit", ""))) is not None,
         "successor frozen source is unresolved")
    artifacts = manifest.get("artifacts")
    need(isinstance(artifacts, Mapping) and set(artifacts) == ARTIFACT_NAMES,
         "successor artifact inventory differs")
    paths = {}
    for name, row in artifacts.items():
        need(isinstance(row, Mapping) and set(row) == {"file", "sha256"}
             and isinstance(row["file"], str)
             and Path(row["file"]).name == row["file"]
             and HEX64.fullmatch(str(row["sha256"])) is not None,
             f"invalid successor artifact: {name}")
        path = packet / row["file"]
        need(path.is_file() and not path.is_symlink() and sha(path) == row["sha256"],
             f"successor artifact changed: {name}")
        paths[name] = path
    need(len(set(paths.values())) == len(paths),
         "successor artifact files must be distinct")
    acceptance = manifest.get("acceptance", {})
    need(acceptance == {
        "state": "accepted",
        "full_suite_receipt_sha256": artifacts["full_suite_receipt"]["sha256"],
        "wheel_sha256": artifacts["wheel"]["sha256"],
        "copied_state_rehearsal_binding_sha256":
            artifacts["copied_state_rehearsal_binding"]["sha256"],
        "health_acceptance_required": True,
    }, "successor acceptance evidence differs")
    transition = load_json(paths["transition_manifest"])
    need(transition.get("status") == "prepared_inert"
         and transition.get("source_commit") == source["commit"],
         "successor transition is not the frozen inert candidate")
    binding = load_json(paths["copied_state_rehearsal_binding"])
    need(binding.get("status") == "passed"
         and binding.get("acceptance_state") == "candidate_evidence_only"
         and binding.get("code_commit") == source["commit"]
         and binding.get("transition_manifest_sha256")
         == artifacts["transition_manifest"]["sha256"],
         "copied-state rehearsal does not bind this successor")
    suite = load_json(paths["full_suite_receipt"])
    try:
        release_acceptance._validate_full_suite(
            paths["full_suite_receipt"], paths["full_suite_log"],
            paths["full_suite_runner"], paths["full_suite_native_result"],
            source_root=Path(source["root"]).resolve(), commit=source["commit"],
            expected_log_sha256=artifacts["full_suite_log"]["sha256"],
            expected_runner_sha256=artifacts["full_suite_runner"]["sha256"],
            expected_native_result_sha256=
                artifacts["full_suite_native_result"]["sha256"],
        )
    except release_acceptance.CandidateError as exc:
        raise SuccessorExecuteError("full suite did not pass for the successor source") from exc
    wheel = load_json(paths["wheel_verification"])
    counts = wheel.get("counts")
    need(wheel.get("code_commit") == source["commit"]
         and Path(wheel.get("source_root", "")).resolve() == Path(source["root"]).resolve()
         and Path(wheel.get("wheel_file", "")).name == paths["wheel"].name
         and wheel.get("wheel_sha256") == artifacts["wheel"]["sha256"]
         and wheel.get("source_bytes_match") is True
         and isinstance(counts, Mapping) and counts
         and all(isinstance(value, int) and not isinstance(value, bool) and value > 0
                 for value in counts.values())
         and isinstance(wheel.get("javascript_checks"), int)
         and not isinstance(wheel.get("javascript_checks"), bool)
         and wheel["javascript_checks"] >= 0,
         "wheel verification does not bind the successor wheel")
    before = load_json(paths["model_config_before_snapshot"])
    after = load_json(paths["model_config_after_snapshot"])
    expected_after, _document, _lane = expected_transition_state(
        packet_root=packet, manifest=transition)
    runtime = manifest.get("runtime", {})
    need(before == load_json(packet / transition["supporting_evidence"]
                             ["baseline_model_snapshot"]["file"])
         and after == expected_after
         and runtime == {"model_config_count_before": len(before),
                         "model_config_count_after": len(after),
                         "backup_keep_latest": 3,
                         "thesis_impact_enabled": False},
         "successor before/after runtime inventory differs")
    health = manifest.get("health_acceptance")
    need(isinstance(health, Mapping)
         and set(health) == {"sample_count", "interval_seconds",
                             "minimum_duration_seconds"}
         and all(isinstance(health.get(name), int)
                 and not isinstance(health.get(name), bool)
                 and health[name] > 0
                 for name in health)
         and health["sample_count"] >= 2
         and ((health["sample_count"] - 1) * health["interval_seconds"]
              >= health["minimum_duration_seconds"]),
         "successor health acceptance window is invalid")
    need(manifest.get("boundaries") == {
        "manifest_publication": False, "model_calls": False,
    }, "successor execution boundaries differ")
    return manifest, paths


class SuccessorOrchestrator(r11.Orchestrator):
    @staticmethod
    def _protected_hash_excluding(root: Path, excluded: set[str]) -> str:
        rows = []
        paths = [path for path in root.glob("*.json")
                 if path.name not in r11.INSTALLER_MANAGED_STATE_JSON
                 and path.name not in excluded]
        for name in ("connector-governance", "governance-decisions", "discovery-plans"):
            directory = root / name
            if directory.is_dir(): paths.extend([directory, *directory.rglob("*")])
        for path in sorted(set(paths)):
            relative = path.relative_to(root).as_posix()
            mode = path.lstat().st_mode & 0o7777
            if path.is_symlink(): rows.append([relative, "symlink", mode, os.readlink(path)])
            elif path.is_file(): rows.append([relative, "file", mode, sha(path)])
            elif path.is_dir(): rows.append([relative, "dir", mode])
            else: raise SuccessorExecuteError("unsupported protected state entry")
        return canonical_hash(rows)

    def live_preflight(self, manifest: Mapping[str, Any], artifacts: Mapping[str, Path]):
        source = Path(manifest["source"]["root"])
        need(source.is_dir() and not source.is_symlink(),
             "successor source root is unavailable")
        actual = subprocess.check_output(
            ["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
        dirty = subprocess.check_output(
            ["git", "-C", str(source), "status", "--porcelain",
             "--untracked-files=all"], text=True)
        need(actual == manifest["source"]["commit"] and not dirty,
             "successor source identity changed")
        need(sha(artifacts["wheel"]) == manifest["acceptance"]["wheel_sha256"],
             "accepted successor wheel changed")
        before = load_json(artifacts["model_config_before_snapshot"])
        need(r11.current_models() == before, "live model inventory differs from successor baseline")
        need(r11.SERVICE_CONFIG.read_bytes() == artifacts["service_config_snapshot"].read_bytes()
             and load_json(r11.SERVICE_CONFIG).get("backup", {}).get("keep_latest") == 3,
             "live service config differs from keep-latest-3 baseline")
        need(r11.OPENCLAW.read_bytes() == artifacts["openclaw_config_snapshot"].read_bytes(),
             "live OpenClaw config differs from successor baseline")
        need(not (r11.STATE / DOCUMENT_CONFIG).exists()
             and not (r11.STATE / LANE_CONFIG).exists(),
             "successor exclusive config target already exists")
        need(load_json(r11.SERVICE_CONFIG).get("thesis_impact", {}).get("enabled") is False
             and not (r11.LAUNCH_AGENTS / "space.lumos.dalton.thesis-impact.plist").exists()
             and not self.loaded("space.lumos.dalton.thesis-impact"),
             "disabled thesis impact authority changed")
        authority_artifacts = dict(artifacts)
        authority_artifacts["model_config_snapshot"] = artifacts["model_config_before_snapshot"]
        authority_artifacts["service_config_before"] = artifacts["service_config_snapshot"]
        authority = r11.verify_runtime_authorities(authority_artifacts)
        plugin = r11.verify_provider_plugin(artifacts["provider_plugin_snapshot"])
        web = load_json(artifacts["web_v6_activation_receipt"])
        selected = web.get("selected_plan", {})
        selected_path = Path(selected.get("path", ""))
        need(web.get("status") == "activated_verified"
             and selected_path.is_file() and not selected_path.is_symlink()
             and sha(selected_path) == selected.get("file_sha256"),
             "live web-v6 authority differs")
        alpha = load_json(artifacts["alpha_v3_activation_receipt"])
        need(alpha.get("status") == "activated_verified"
             and alpha.get("large_state_copied") is False
             and all(Path(path).is_file() and not Path(path).is_symlink()
                     and sha(Path(path)) == expected
                     for path, expected in alpha.get("owned_targets", {}).items()),
             "live Alpha authority differs")
        unreviewed = sorted(name for name in os.environ
                            if name.startswith("DALTON_") or name == "DRAIN_TIMEOUT")
        need(not unreviewed,
             "unreviewed installer controls are present: " + ", ".join(unreviewed))
        executables = [r11.VENV / "bin/python", r11.VENV / "bin/dalton-backup",
                       source / "deploy/macos/install.sh"]
        need(all(path.exists() and path.resolve().is_file()
                 and os.access(path, os.X_OK) for path in executables),
             "deployment executable is unavailable")
        return {"source": source, "authority": authority,
                "provider_plugin_tree_sha256": plugin}

    def install_successor(self, source: Path, manifest: Mapping[str, Any],
                          artifacts: Mapping[str, Path]) -> None:
        self.artifacts = dict(artifacts)
        self.artifacts["service_config_before"] = artifacts["service_config_snapshot"]
        self.artifacts["service_config_after"] = artifacts["service_config_snapshot"]
        self.mutations_started = True
        self.command([str(r11.VENV / "bin/python"), "-m", "pip", "install",
                      "--disable-pip-version-check", "--no-deps", "--force-reinstall",
                      str(artifacts["wheel"])])
        transition_receipt = self.rollback_root / "successor-config-transition-receipt.json"
        apply_transition(
            packet_root=self.packet, state_dir=r11.STATE,
            manifest_path=artifacts["transition_manifest"],
            expected_manifest_sha256=sha(artifacts["transition_manifest"]),
            receipt_path=transition_receipt,
            accepted_evidence=manifest["acceptance"],
        )
        rendered = self.rollback_root / "reviewed-rendered-plists"
        self.command([str(r11.VENV / "bin/python"), "-m", "dalton_core.macos_launchagent",
                      "--launch-agents-dir", str(rendered),
                      "--python-env-bin", str(r11.VENV / "bin"),
                      "--state-dir", str(r11.STATE), "--config", str(r11.SERVICE_CONFIG),
                      "--log-dir", str(r11.HOME / "Library/Logs/Dalton")])
        initial_path = self.rollback_root / "initial-state.json"
        initial = load_json(initial_path)
        initial["reviewed_rendered_plist_sha256"] = {
            label: sha(rendered / f"{label}.plist") for label in r11.LABELS
            if (rendered / f"{label}.plist").is_file()}
        temporary = initial_path.with_name(".initial-state.successor-rendered")
        temporary.write_text(json.dumps(initial, indent=2) + "\n")
        os.replace(temporary, initial_path)
        env = dict(os.environ)
        env.update({"DALTON_STARTUP_TIMEOUT_SECONDS": "900", "DRAIN_TIMEOUT": "600"})
        self.command(["/bin/zsh", str(source / "deploy/macos/install.sh")], env=env)
        need(subprocess.check_output(
                 ["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
             == manifest["source"]["commit"]
             and not subprocess.check_output(
                 ["git", "-C", str(source), "status", "--porcelain",
                  "--untracked-files=all"], text=True),
             "successor source changed during stopped-window installation")

    def verify_successor(self, manifest: Mapping[str, Any], artifacts: Mapping[str, Path]):
        transition = load_json(artifacts["transition_manifest"])
        expected_models, expected_document, expected_lane = expected_transition_state(
            packet_root=self.packet, manifest=transition)
        need(r11.current_models() == expected_models,
             "installed successor model inventory differs")
        need(load_json(r11.STATE / DOCUMENT_CONFIG) == expected_document
             and load_json(r11.STATE / LANE_CONFIG) == expected_lane,
             "installed successor document configuration differs")
        need(r11.SERVICE_CONFIG.read_bytes() == artifacts["service_config_snapshot"].read_bytes()
             and r11.OPENCLAW.read_bytes() == artifacts["openclaw_config_snapshot"].read_bytes(),
             "installer changed preserved service or OpenClaw config")
        need(load_json(r11.SERVICE_CONFIG).get("thesis_impact", {}).get("enabled") is False
             and not (r11.LAUNCH_AGENTS / "space.lumos.dalton.thesis-impact.plist").exists()
             and not self.loaded("space.lumos.dalton.thesis-impact"),
             "installer enabled thesis impact")
        writer = plistlib.loads((r11.LAUNCH_AGENTS /
            "space.lumos.dalton.writer.plist").read_bytes())["ProgramArguments"]
        need("--mission-document-research-lane" in writer,
             "installed writer does not consume mission document lane")
        roots = list((r11.VENV / "lib").glob("python*/site-packages"))
        need(len(roots) == 1, "installed site-packages inventory differs")
        with zipfile.ZipFile(artifacts["wheel"]) as archive:
            files = {name: archive.read(name) for name in archive.namelist()
                     if name.startswith("dalton_core/")
                     and Path(name).suffix in {".py", ".sql", ".json", ".html"}}
        actual = {path.relative_to(roots[0]).as_posix()
                  for path in (roots[0] / "dalton_core").rglob("*")
                  if path.is_file() and path.suffix in {".py", ".sql", ".json", ".html"}}
        need(files and actual == set(files)
             and all((roots[0] / name).read_bytes() == data for name, data in files.items()),
             "installed successor runtime bytes differ from wheel")
        need(self.rollback_root is not None, "rollback authority is unavailable")
        initial = load_json(self.rollback_root / "initial-state.json")
        need([label for label in r11.LABELS if self.loaded(label)] == self.initially_loaded,
             "installed service set differs from stopped-window precondition")
        for label, expected in initial.get("reviewed_rendered_plist_sha256", {}).items():
            target = r11.LAUNCH_AGENTS / f"{label}.plist"
            need(target.is_file() and not target.is_symlink() and sha(target) == expected,
                 f"installed LaunchAgent bytes differ: {label}")
        need(r11.verify_provider_plugin(artifacts["provider_plugin_snapshot"]),
             "provider plugin authority differs")
        web = load_json(artifacts["web_v6_activation_receipt"])
        selected = web.get("selected_plan", {})
        selected_path = Path(selected.get("path", ""))
        need(selected_path.is_file() and not selected_path.is_symlink()
             and sha(selected_path) == selected.get("file_sha256"),
             "web-v6 authority changed")
        alpha = load_json(artifacts["alpha_v3_activation_receipt"])
        need(all(Path(path).is_file() and not Path(path).is_symlink()
                 and sha(Path(path)) == expected
                 for path, expected in alpha.get("owned_targets", {}).items()),
             "Alpha authority changed")
        authority_artifacts = dict(artifacts)
        authority_artifacts["model_config_snapshot"] = artifacts["model_config_after_snapshot"]
        authority_artifacts["service_config_before"] = artifacts["service_config_snapshot"]
        authority = r11.verify_runtime_authorities(authority_artifacts)
        health = self.command([str(r11.VENV / "bin/dalton-health"), "--config",
                               str(r11.SERVICE_CONFIG), "--max-age-seconds", "45"])
        need(r11.load_json_bytes(health.stdout).get("ok") is True,
             "installed successor runtime is unhealthy")
        return {"model_config_count": len(expected_models), "runtime_files": len(files),
                "authority": authority, "writer_lane_enabled": True,
                "thesis_impact_enabled": False, "backup_keep_latest": 3}

    def rollback(self):
        """Remove/restore exact successor targets, then run the R11a rollback."""

        if not self.mutations_started or self.artifacts is None or self.rollback_root is None:
            return super().rollback()
        transition = load_json(self.artifacts["transition_manifest"])
        conflicts = []
        for row in reversed(transition["targets"]):
            target = r11.STATE / row["name"]
            after = (self.packet / row["after"]["file"]).read_bytes()
            try:
                if (not target.is_file() or target.is_symlink()
                        or target.read_bytes() != after):
                    conflicts.append(row["name"]); continue
                if row["kind"] == "exclusive_add":
                    target.unlink()
                else:
                    before = (self.packet / row["before"]["file"]).read_bytes()
                    temporary = target.with_name("." + target.name + ".successor-rollback")
                    try:
                        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                                     target.stat().st_mode & 0o7777)
                        with os.fdopen(fd, "wb") as stream:
                            stream.write(before); stream.flush(); os.fsync(stream.fileno())
                        os.replace(temporary, target)
                    finally:
                        temporary.unlink(missing_ok=True)
            except OSError:
                conflicts.append(row["name"])
        directory_fd = os.open(r11.STATE, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        if conflicts:
            excluded = set(conflicts)
            rollback_state = self.rollback_root / "state-files"
            need(self._protected_hash_excluding(r11.STATE, excluded)
                 == self._protected_hash_excluding(rollback_state, excluded),
                 "protected owner state changed outside successor config targets")
            initial_path = self.rollback_root / "initial-state.json"
            initial = load_json(initial_path)
            initial["protected_state_sha256"] = r11.protected_state_hash(r11.STATE)
            temporary = initial_path.with_name(".initial-state.successor-conflicts")
            try:
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(initial, stream, indent=2); stream.write("\n")
                    stream.flush(); os.fsync(stream.fileno())
                os.replace(temporary, initial_path)
            finally:
                temporary.unlink(missing_ok=True)
        result = super().rollback()
        return {**result, "preserved_concurrent_config_targets": sorted(conflicts)}


def execute(packet: Path, expected_manifest_sha256: str, log_path: Path,
            receipt_path: Path, installed_path: Path | None, *, do_execute: bool):
    manifest_path = packet / "release-manifest.candidate.json"
    need(HEX64.fullmatch(expected_manifest_sha256) is not None
         and manifest_path.is_file() and sha(manifest_path) == expected_manifest_sha256,
         "explicit successor manifest hash differs")
    manifest, artifacts = packet_preflight(packet)
    if not do_execute:
        with open(os.devnull, "w") as sink:
            result = SuccessorOrchestrator(packet, sink).live_preflight(manifest, artifacts)
        return {"status": "reviewed_preflight_passed", "live_mutation": False,
                "source": str(result["source"]), **{k: v for k, v in result.items()
                                                    if k != "source"}}
    need(installed_path is not None and not log_path.exists()
         and not receipt_path.exists() and not installed_path.exists(),
         "execution outputs must be new")
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    started = datetime.now(timezone.utc).isoformat()
    with os.fdopen(fd, "w", encoding="utf-8") as log:
        worker = SuccessorOrchestrator(packet, log)
        try:
            preflight = worker.live_preflight(manifest, artifacts)
            worker.disk_preflight(preflight["source"])
            worker.stop_and_backup(preflight["source"])
            worker.install_successor(preflight["source"], manifest, artifacts)
            verified = worker.verify_successor(manifest, artifacts)
            installed = {"schema_version": "successor-installed-verification-0.1",
                         "status": "installed_bytes_verified_runtime_pending",
                         "source_commit": manifest["source"]["commit"],
                         "candidate_manifest_sha256": expected_manifest_sha256,
                         "wheel_sha256": sha(artifacts["wheel"]), **verified}
            r11.exclusive_json(installed_path, installed)
            outcome = {"status": "installer_finished_runtime_health_pending",
                       "exit_code": 0, "installed": verified,
                       "installed_verification": installed_path.name,
                       "installed_verification_sha256": sha(installed_path),
                       "rollback": {"status": "not_required"}}
        except (Exception, KeyboardInterrupt) as exc:
            worker.note(f"FAIL {type(exc).__name__}: {exc}")
            try:
                recovery = worker.rollback()
            except Exception as recovery_exc:
                recovery = {"status": "rollback_failed",
                            "error": f"{type(recovery_exc).__name__}: {recovery_exc}"}
            outcome = {"status": "deployment_failed", "exit_code": 1,
                       "error": f"{type(exc).__name__}: {exc}", "rollback": recovery}
        if worker.rollback_root is not None:
            outcome["fresh_rollback_snapshot"] = {
                "path": str(worker.rollback_root),
                "database_snapshot_id": worker.snapshot_id}
    receipt = {"schema_version": "successor-stopped-window-execution-0.1",
               "source_commit": manifest["source"]["commit"],
               "candidate_manifest_sha256": expected_manifest_sha256,
               "started_at": started, "finished_at": datetime.now(timezone.utc).isoformat(),
               "log": log_path.name, "log_sha256": sha(log_path),
               "manifest_modified": False, **outcome}
    r11.exclusive_json(receipt_path, receipt)
    return receipt


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-template", type=Path)
    parser.add_argument("--packet", type=Path)
    parser.add_argument("--expected-manifest-sha256")
    parser.add_argument("--log", type=Path)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--installed-verification", type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    if args.write_template is not None:
        need(args.packet is None and not args.execute and not args.write_template.exists(),
             "template generation must be inert and exclusive")
        args.write_template.write_text(json.dumps(template(), indent=2) + "\n")
        print(json.dumps({"status": "incomplete_template_written"})); return 0
    need(args.packet is not None and args.expected_manifest_sha256 is not None,
         "preflight requires packet and exact manifest SHA-256")
    need(not args.execute or all(value is not None for value in (
        args.log, args.receipt, args.installed_verification)),
        "execute requires log, receipt and installed verification outputs")
    result = execute(
        args.packet.resolve(), args.expected_manifest_sha256,
        args.log.resolve() if args.log else Path("/nonexistent-log"),
        args.receipt.resolve() if args.receipt else Path("/nonexistent-receipt"),
        args.installed_verification.resolve() if args.installed_verification else None,
        do_execute=args.execute)
    print(json.dumps(result)); return int(result.get("exit_code", 0))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (SuccessorExecuteError, OSError, subprocess.SubprocessError,
            zipfile.BadZipFile) as exc:
        raise SystemExit(f"STOP: {exc}") from exc
