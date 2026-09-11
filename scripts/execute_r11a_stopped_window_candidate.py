#!/usr/bin/env python3
"""Execute the concrete R11a stopped-window deployment with rollback.

The default mode performs a read-only packet/live preflight.  ``--execute``
requires the caller to repeat the staged candidate manifest SHA-256, creates a
fresh rollback snapshot, and writes a full private log plus an exclusive
receipt.  It never edits or publishes the candidate manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence, TextIO


COMMIT = "424c22529d5317c4c5c9dc8ba88f37299cea3f49"
WHEEL_SHA256 = "418bf77f7b5285f9e53e041bdcf9d36463e57a7fa0bd4ce368ded2ec1e50d046"
PACKET_SCHEMA = "r11a-ops-packet-candidate-0.1"
HEX64 = re.compile(r"[0-9a-f]{64}")
HOME = Path.home()
DALTON = HOME / "Library/Application Support/Dalton"
STATE = DALTON / "state/dalton-core"
CONFIG_DIR = DALTON / "config"
SERVICE_CONFIG = CONFIG_DIR / "service.json"
RUNTIME = DALTON / "runtime"
VENV = RUNTIME / "venv"
OPENCLAW = HOME / ".openclaw/openclaw.json"
LAUNCH_AGENTS = HOME / "Library/LaunchAgents"
LABELS = ("space.lumos.dalton.writer", "space.lumos.dalton.controller",
          "space.lumos.dalton.control", "space.lumos.dalton.thesis-impact")
# install.sh owns this discovery input and may create it when a gateway is
# present.  It is deliberately outside the owner/governance concurrency guard;
# rollback never restores state JSON, so an installer-written value is kept.
INSTALLER_MANAGED_STATE_JSON = frozenset({"model-catalog-sync.json"})


class ExecuteError(RuntimeError):
    pass


def need(ok: Any, reason: str) -> None:
    if not ok:
        raise ExecuteError(reason)


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_hash(value: Any) -> str:
    wire = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    return hashlib.sha256(wire.encode()).hexdigest()


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ExecuteError(f"invalid JSON: {path}") from exc


def exclusive_json(path: Path, value: Mapping[str, Any]) -> None:
    need(path.parent.is_dir() and not path.parent.is_symlink(), "receipt parent is unavailable")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2); stream.write("\n")
        stream.flush(); os.fsync(stream.fileno())


def current_models() -> dict[str, Any]:
    return {path.name: load_json(path) for path in sorted(STATE.glob("*model-config.json"))}


def tree_hash(root: Path) -> str:
    """Hash names, modes, symlink targets and file bytes for one private tree."""
    rows = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        mode = path.lstat().st_mode & 0o7777
        if path.is_symlink(): rows.append([relative, "symlink", mode, os.readlink(path)])
        elif path.is_file(): rows.append([relative, "file", mode, sha(path)])
        elif path.is_dir(): rows.append([relative, "dir", mode])
        else: raise ExecuteError(f"unsupported rollback tree entry: {path}")
    return canonical_hash(rows)


def tree_hash_excluding(root: Path, excluded: set[str]) -> str:
    rows = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if relative in excluded: continue
        mode = path.lstat().st_mode & 0o7777
        if path.is_symlink(): rows.append([relative, "symlink", mode, os.readlink(path)])
        elif path.is_file(): rows.append([relative, "file", mode, sha(path)])
        elif path.is_dir(): rows.append([relative, "dir", mode])
        else: raise ExecuteError(f"unsupported tree entry: {path}")
    return canonical_hash(rows)


def protected_state_hash(root: Path) -> str:
    rows = []
    paths = [path for path in root.glob("*.json")
             if path.name not in INSTALLER_MANAGED_STATE_JSON]
    for name in ("connector-governance", "governance-decisions", "discovery-plans"):
        directory = root / name
        if directory.is_dir(): paths.extend([directory, *directory.rglob("*")])
    for path in sorted(set(paths)):
        relative = path.relative_to(root).as_posix(); mode = path.lstat().st_mode & 0o7777
        if path.is_symlink(): rows.append([relative, "symlink", mode, os.readlink(path)])
        elif path.is_file(): rows.append([relative, "file", mode, sha(path)])
        elif path.is_dir(): rows.append([relative, "dir", mode])
        else: raise ExecuteError(f"unsupported protected state entry: {path}")
    return canonical_hash(rows)


def stored_hash(value: Mapping[str, Any]) -> str:
    body = {key: item for key, item in value.items() if key != "content_hash"}
    wire = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(wire.encode()).hexdigest()


def verify_runtime_authorities(artifacts: Mapping[str, Path]) -> dict[str, Any]:
    configs = load_json(artifacts["model_config_snapshot"])
    router_path = Path(next(iter(configs.values()))["model_router_db"])
    budget_path = Path(next(iter(configs.values()))["budget_db"])
    router = sqlite3.connect(f"file:{router_path}?mode=ro", uri=True); router.row_factory = sqlite3.Row
    budget = sqlite3.connect(f"file:{budget_path}?mode=ro", uri=True); budget.row_factory = sqlite3.Row
    try:
        policies = {}
        budget_policies = {}
        for name, config in configs.items():
            policy_ref = config["routing_policy_ref"]
            row = router.execute("SELECT policy_hash,policy_json FROM model_routing_policy_versions WHERE policy_version_ref=?",
                                 (policy_ref,)).fetchone()
            need(row is not None, f"live model route is absent: {name}")
            policy = json.loads(row["policy_json"])
            need(policy.get("content_hash") == row["policy_hash"] == stored_hash(policy),
                 f"live model route hash differs: {name}")
            policies[name] = policy_ref
            slots = config.get("credential_slot_refs")
            need(isinstance(slots, list) and slots and len(slots) == len(set(slots))
                 and all(isinstance(slot, str) and slot.startswith("credential-slot:openclaw:") for slot in slots),
                 f"credential slot authority differs: {name}")
            budget_ref = config["budget_policy_ref"]
            if budget_ref not in budget_policies:
                budget_row = budget.execute(
                    "SELECT content_hash,record_json FROM thesis_impact_budget_policies WHERE policy_version_id=?",
                    (budget_ref,)).fetchone()
                need(budget_row is not None, f"live budget policy is absent: {budget_ref}")
                record = json.loads(budget_row["record_json"])
                need(record.get("content_hash") == budget_row["content_hash"] == stored_hash(record),
                     f"live budget policy hash differs: {budget_ref}")
                budget_policies[budget_ref] = budget_row["content_hash"]
    finally:
        router.close(); budget.close()
    mission_receipt = load_json(artifacts["mission_authority_snapshot"])
    core_path = Path(load_json(artifacts["service_config_before"])["core_db"])
    core = sqlite3.connect(f"file:{core_path}?mode=ro", uri=True); core.row_factory = sqlite3.Row
    try:
        rows = core.execute(
            "SELECT p.mission_version_id,p.content_hash AS pointer_hash,v.record_json,v.content_hash "
            "FROM coverage_mission_pointer p JOIN coverage_mission_versions v "
            "ON v.mission_version_id=p.mission_version_id").fetchall()
        need(len(rows) == 1, "live mission pointer inventory differs")
        mission = json.loads(rows[0]["record_json"])
        need(rows[0]["mission_version_id"] == mission_receipt.get("active_version_ref")
             and rows[0]["pointer_hash"] == rows[0]["content_hash"] == mission_receipt.get("active_hash")
             and mission.get("content_hash") == rows[0]["content_hash"] == stored_hash(mission),
             "live mission pointer/version/hash differs")
    finally: core.close()
    return {"routing_policies": policies, "budget_policies": budget_policies,
            "mission_version_ref": mission_receipt["active_version_ref"],
            "mission_hash": mission_receipt["active_hash"]}


def packet_preflight(packet: Path) -> tuple[dict[str, Any], dict[str, Path]]:
    manifest_path = packet / "release-manifest.candidate.json"
    need(packet.is_dir() and not packet.is_symlink() and manifest_path.is_file()
         and not manifest_path.is_symlink(), "candidate packet is unavailable")
    manifest = load_json(manifest_path)
    unsigned = dict(manifest); declared_content_hash = unsigned.pop("content_hash", None)
    need(manifest.get("schema_version") == PACKET_SCHEMA
         and manifest.get("release_ref") == "foundation-followup-r11a"
         and manifest.get("status") == "staged_pending_owner_acceptance"
         and manifest.get("acceptance_state") == "pending"
         and manifest.get("deployment_state") == "not_started"
         and manifest.get("source", {}).get("commit") == COMMIT
         and declared_content_hash == canonical_hash(unsigned), "candidate manifest differs")
    artifacts: dict[str, Path] = {}
    for name, row in manifest.get("artifacts", {}).items():
        need(isinstance(row, Mapping) and set(row) == {"file", "sha256"}
             and isinstance(row.get("file"), str) and Path(row["file"]).name == row["file"]
             and isinstance(row.get("sha256"), str) and HEX64.fullmatch(row["sha256"]),
             f"invalid candidate artifact: {name}")
        path = packet / row["file"]
        need(path.is_file() and not path.is_symlink() and sha(path) == row["sha256"],
             f"candidate artifact changed: {name}")
        artifacts[name] = path
    helpers = manifest.get("helpers")
    need(isinstance(helpers, Mapping), "candidate helper inventory is absent")
    for name, expected in helpers.items():
        path = packet / name
        need(Path(name).name == name and isinstance(expected, str) and HEX64.fullmatch(expected)
             and path.is_file() and not path.is_symlink() and sha(path) == expected,
             f"candidate helper changed: {name}")
    need(manifest.get("runtime_configuration", {}).get("model_config_count") == 15,
         "candidate is not bound to the activated 15-config runtime")
    return manifest, artifacts


def verify_provider_plugin(snapshot_path: Path) -> str:
    snapshot = load_json(snapshot_path); root = Path(snapshot.get("root", ""))
    rows = snapshot.get("files")
    need(snapshot.get("schema_version") == "r11a-provider-plugin-snapshot-0.1"
         and snapshot.get("openclaw_config_sha256") == sha(OPENCLAW)
         and isinstance(rows, list) and rows and canonical_hash(rows) == snapshot.get("tree_sha256")
         and root.is_dir() and not root.is_symlink(), "provider plugin authority differs")
    expected = set()
    for row in rows:
        relative = Path(row.get("path", ""))
        need(not relative.is_absolute() and ".." not in relative.parts, "provider plugin path is unsafe")
        path = root / relative; expected.add(relative.as_posix())
        need(path.is_file() and not path.is_symlink() and sha(path) == row.get("sha256"),
             "provider plugin bytes changed")
    actual = {path.relative_to(root).as_posix() for path in root.rglob("*")
              if path.is_file() and "__pycache__" not in path.parts}
    need(actual == expected, "provider plugin inventory changed")
    return snapshot["tree_sha256"]


class Orchestrator:
    def __init__(self, packet: Path, log: TextIO):
        self.packet = packet
        self.log = log
        self.domain = f"gui/{os.getuid()}"
        self.initially_loaded: list[str] = []
        self.stopped = False
        self.mutations_started = False
        self.rollback_root: Path | None = None
        self.snapshot_id: str | None = None
        self.source: Path | None = None
        self.artifacts: Mapping[str, Path] | None = None

    def note(self, value: str) -> None:
        self.log.write(f"[{datetime.now(timezone.utc).isoformat()}] {value}\n")
        self.log.flush()

    def command(self, argv: Sequence[str], *, timeout: float | None = None,
                env: Mapping[str, str] | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
        self.note("RUN " + json.dumps(list(argv)))
        result = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout,
                                env=None if env is None else dict(env))
        if result.stdout: self.log.write(result.stdout)
        if result.stderr: self.log.write(result.stderr)
        self.log.flush()
        if check and result.returncode:
            raise ExecuteError(f"command failed ({result.returncode}): {argv[0]}")
        return result

    def loaded(self, label: str) -> bool:
        return self.command(["/bin/launchctl", "print", f"{self.domain}/{label}"], check=False).returncode == 0

    def stop(self, label: str) -> None:
        if self.loaded(label):
            self.command(["/bin/launchctl", "bootout", f"{self.domain}/{label}"])
        for _ in range(150):
            if not self.loaded(label): return
            time.sleep(.2)
        raise ExecuteError(f"service did not stop: {label}")

    def start(self, label: str) -> None:
        plist = LAUNCH_AGENTS / f"{label}.plist"
        need(plist.is_file() and not plist.is_symlink(), f"restored plist unavailable: {label}")
        if not self.loaded(label): self.command(["/bin/launchctl", "bootstrap", self.domain, str(plist)])
        self.command(["/bin/launchctl", "enable", f"{self.domain}/{label}"])
        self.command(["/bin/launchctl", "kickstart", "-k", f"{self.domain}/{label}"])

    def live_preflight(self, manifest: Mapping[str, Any], artifacts: Mapping[str, Path]) -> dict[str, Any]:
        source = Path(manifest["source"]["root"])
        need(subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip() == COMMIT
             and not subprocess.check_output(["git", "-C", str(source), "status", "--porcelain", "--untracked-files=all"], text=True),
             "frozen source identity changed")
        need(sha(artifacts["wheel"]) == WHEEL_SHA256, "accepted wheel changed")
        need(SERVICE_CONFIG.is_file() and not SERVICE_CONFIG.is_symlink()
             and SERVICE_CONFIG.read_bytes() == artifacts["service_config_before"].read_bytes(),
             "live service config differs from stopped-window precondition")
        service = load_json(SERVICE_CONFIG)
        need(service.get("thesis_impact", {}).get("enabled") is False,
             "thesis impact must remain disabled")
        need(current_models() == load_json(artifacts["model_config_snapshot"]),
             "live 15-model configuration differs")
        need(OPENCLAW.is_file() and not OPENCLAW.is_symlink()
             and OPENCLAW.read_bytes() == artifacts["openclaw_config_snapshot"].read_bytes(),
             "live OpenClaw configuration differs")
        runtime_snapshot = load_json(artifacts["runtime_snapshot_receipt"])
        need(runtime_snapshot.get("status") == "current_web_provider_configuration_snapshotted"
             and runtime_snapshot.get("model_catalog_and_model_broker_subtree_unchanged") is True,
             "current web-provider snapshot authority differs")
        plugin_tree_sha256 = verify_provider_plugin(artifacts["provider_plugin_snapshot"])
        authority = verify_runtime_authorities(artifacts)
        web = load_json(artifacts["web_v6_activation_receipt"])
        selected = web.get("selected_plan", {})
        selected_path = Path(selected.get("path", ""))
        need(web.get("status") == "activated_verified" and selected_path.is_file()
             and not selected_path.is_symlink() and sha(selected_path) == selected.get("file_sha256")
             and selected_path.name == "us-it-services-web-search-v6-host-recovery.json",
             "live web-v6 authority differs")
        alpha = load_json(artifacts["alpha_v3_activation_receipt"])
        need(alpha.get("status") == "activated_verified" and alpha.get("large_state_copied") is False,
             "Alpha v3 activation proof differs")
        for raw_path, expected in alpha.get("owned_targets", {}).items():
            target = Path(raw_path)
            need(target.is_file() and not target.is_symlink() and sha(target) == expected,
                 "live Alpha v3 authority differs")
        need(not (LAUNCH_AGENTS / "space.lumos.dalton.thesis-impact.plist").exists()
             and not self.loaded("space.lumos.dalton.thesis-impact"),
             "disabled thesis-impact service is unexpectedly installed or running")
        unreviewed = sorted(name for name in os.environ
                            if name.startswith("DALTON_") or name == "DRAIN_TIMEOUT")
        need(not unreviewed, "unreviewed installer controls are present: " + ", ".join(unreviewed))
        required = [VENV / "bin/python", VENV / "bin/dalton-backup", source / "deploy/macos/install.sh"]
        need(all(path.exists() and path.resolve().is_file() and os.access(path, os.X_OK) for path in required),
             "deployment executable is unavailable")
        return {"source": source, "service_config_sha256": sha(SERVICE_CONFIG),
                "model_config_count": len(current_models()), "openclaw_sha256": sha(OPENCLAW),
                "provider_plugin_tree_sha256": plugin_tree_sha256, "authority": authority,
                "thesis_impact_enabled": False}

    def disk_preflight(self, source: Path) -> None:
        self.command(["/opt/homebrew/bin/python3", str(source / "src/dalton_core/install_disk_preflight.py"),
                      "--repo-root", str(source), "--dalton-root", str(DALTON),
                      "--backup-copies", "2", "--backup-root", str(self.packet)])

    def stop_and_backup(self, source: Path) -> None:
        self.source = source
        self.initially_loaded = [label for label in LABELS if self.loaded(label)]
        # From the first bootout onward every failure must restore precisely
        # this captured loaded set, including a failure part-way through stop.
        self.stopped = True
        for label in reversed(LABELS[1:]): self.stop(label)
        self.command(["/opt/homebrew/bin/python3", str(source / "src/dalton_core/controller_singleton.py"),
                      "--check", "--config", str(SERVICE_CONFIG)])
        self.command(["/opt/homebrew/bin/python3", str(source / "src/dalton_core/launch_drain.py"),
                      "--state-dir", str(STATE), "--timeout", "600"])
        self.stop(LABELS[0])
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        rollback = self.packet / f"deploy-rollback-{stamp}"
        rollback.mkdir(mode=0o700); self.rollback_root = rollback
        shutil.copytree(VENV, rollback / "venv", symlinks=True)
        shutil.copytree(CONFIG_DIR, rollback / "config", symlinks=True)
        state_files = rollback / "state-files"; state_files.mkdir(mode=0o700)
        for path in STATE.glob("*.json"): shutil.copy2(path, state_files / path.name)
        for name in ("connector-governance", "governance-decisions", "discovery-plans"):
            if (STATE / name).is_dir(): shutil.copytree(STATE / name, state_files / name, symlinks=True)
        plists = rollback / "plists"; plists.mkdir(mode=0o700)
        plist_inventory = {}
        for label in LABELS:
            path = LAUNCH_AGENTS / f"{label}.plist"; plist_inventory[label] = path.is_file()
            if path.is_file(): shutil.copy2(path, plists / path.name)
        databases = sorted(STATE.glob("*.sqlite")); need(databases, "no authority databases found")
        backup_tree_hashes = {"venv": tree_hash(rollback / "venv"),
                              "config": tree_hash(rollback / "config"),
                              "state-files": tree_hash(state_files), "plists": tree_hash(plists)}
        protected_state_sha256 = protected_state_hash(STATE)
        need(backup_tree_hashes["venv"] == tree_hash(VENV)
             and backup_tree_hashes["config"] == tree_hash(CONFIG_DIR)
             and protected_state_sha256 == protected_state_hash(state_files),
             "runtime or config changed during rollback copy")
        (rollback / "initial-state.json").write_text(json.dumps({"loaded": self.initially_loaded,
            "plists": plist_inventory,
            "plist_sha256": {label: sha(LAUNCH_AGENTS / f"{label}.plist")
                              for label, present in plist_inventory.items() if present},
            "databases": [path.name for path in databases],
            "protected_state_sha256": protected_state_sha256,
            "backup_tree_hashes": backup_tree_hashes}, indent=2) + "\n")
        argv = [str(VENV / "bin/dalton-backup"), "create", "--backup-root", str(rollback / "databases")]
        for path in databases: argv += ["--database", f"{path.stem}={path}"]
        result = self.command(argv); snapshot = json.loads(result.stdout); self.snapshot_id = snapshot["snapshot_id"]
        self.command([str(VENV / "bin/dalton-backup"), "verify-restore", "--backup-root", str(rollback / "databases"),
                      "--snapshot-id", self.snapshot_id, "--restore-root", str(rollback / "restore")])

    def install(self, source: Path, artifacts: Mapping[str, Path]) -> None:
        self.artifacts = artifacts
        self.mutations_started = True
        self.command([str(VENV / "bin/python"), "-m", "pip", "install", "--disable-pip-version-check",
                      "--no-deps", "--force-reinstall", str(artifacts["wheel"])])
        retention = self.packet / "configure_backup_retention_stopped_window.py"
        self.command([str(VENV / "bin/python"), str(retention), "--service-config", str(SERVICE_CONFIG),
                      "--expected-before-sha256", sha(artifacts["service_config_before"]),
                      "--receipt", str(self.rollback_root / "retention-install-receipt.json")])
        env = dict(os.environ)
        env.update({"DALTON_STARTUP_TIMEOUT_SECONDS": "900", "DRAIN_TIMEOUT": "600"})
        self.command(["/bin/zsh", str(source / "deploy/macos/install.sh")], env=env)

    def verify_installed(self, artifacts: Mapping[str, Path]) -> dict[str, Any]:
        need(load_json(SERVICE_CONFIG) == load_json(artifacts["service_config_after"]),
             "installed service config is not the reviewed keep-latest-3 value")
        need(current_models() == load_json(artifacts["model_config_snapshot"]), "installed model configs changed")
        need(OPENCLAW.read_bytes() == artifacts["openclaw_config_snapshot"].read_bytes(), "OpenClaw config changed")
        plugin_tree_sha256 = verify_provider_plugin(artifacts["provider_plugin_snapshot"])
        authority = verify_runtime_authorities(artifacts)
        web = load_json(artifacts["web_v6_activation_receipt"]); selected = web.get("selected_plan", {})
        selected_path = Path(selected.get("path", ""))
        need(selected_path.is_file() and sha(selected_path) == selected.get("file_sha256"),
             "install changed web-v6 authority")
        alpha = load_json(artifacts["alpha_v3_activation_receipt"])
        need(all(Path(path).is_file() and sha(Path(path)) == expected
                 for path, expected in alpha.get("owned_targets", {}).items()),
             "install changed Alpha v3 authority")
        need(load_json(SERVICE_CONFIG).get("thesis_impact", {}).get("enabled") is False
             and not (LAUNCH_AGENTS / "space.lumos.dalton.thesis-impact.plist").exists()
             and not self.loaded("space.lumos.dalton.thesis-impact"), "install enabled thesis impact")
        installed_root = next((VENV / "lib").glob("python*/site-packages"), None)
        need(installed_root is not None, "installed site-packages unavailable")
        with zipfile.ZipFile(artifacts["wheel"]) as archive:
            files = {name: archive.read(name) for name in archive.namelist()
                     if name.startswith("dalton_core/") and Path(name).suffix in {".py", ".sql", ".json", ".html"}}
        need(files and all((installed_root / name).read_bytes() == body for name, body in files.items()),
             "installed Dalton bytes differ from accepted wheel")
        actual_files = {path.relative_to(installed_root).as_posix()
                        for path in (installed_root / "dalton_core").rglob("*")
                        if path.is_file() and path.suffix in {".py", ".sql", ".json", ".html"}}
        need(actual_files == set(files), "installed Dalton runtime inventory has stale or missing files")
        health = self.command([str(VENV / "bin/dalton-health"), "--config", str(SERVICE_CONFIG),
                               "--max-age-seconds", "45"])
        need(load_json_bytes(health.stdout).get("ok") is True, "installed runtime is unhealthy")
        return {"runtime_files": len(files), "model_configs": len(current_models()),
                "service_config_sha256": sha(SERVICE_CONFIG), "openclaw_sha256": sha(OPENCLAW),
                "provider_plugin_tree_sha256": plugin_tree_sha256, "authority": authority,
                "thesis_impact_enabled": False}

    def restart_initial(self) -> None:
        for label in LABELS:
            if label in self.initially_loaded: self.start(label)
        if "space.lumos.dalton.controller" in self.initially_loaded:
            deadline = time.monotonic() + 900
            while time.monotonic() < deadline:
                result = self.command([str(VENV / "bin/dalton-health"), "--config", str(SERVICE_CONFIG),
                                       "--max-age-seconds", "45"], check=False)
                if result.returncode == 0:
                    try:
                        if load_json_bytes(result.stdout).get("ok") is True: return
                    except ExecuteError: pass
                time.sleep(2)
            raise ExecuteError("restored runtime did not become healthy")

    def rollback(self) -> dict[str, Any]:
        if not self.stopped: return {"status": "not_required_before_stop"}
        if not self.mutations_started:
            self.restart_initial(); return {"status": "initial_services_restored", "bytes_restored": False}
        need(self.rollback_root is not None and self.snapshot_id is not None, "rollback snapshot is incomplete")
        for label in reversed(LABELS): self.stop(label)
        need(self.source is not None, "rollback source is unavailable")
        self.command(["/opt/homebrew/bin/python3", str(self.source / "src/dalton_core/controller_singleton.py"),
                      "--check", "--config", str(SERVICE_CONFIG)])
        self.command(["/opt/homebrew/bin/python3", str(self.source / "src/dalton_core/launch_drain.py"),
                      "--state-dir", str(STATE), "--timeout", "600"])
        rollback = self.rollback_root
        initial = load_json(rollback / "initial-state.json")
        for name, expected in initial["backup_tree_hashes"].items():
            need(tree_hash(rollback / name) == expected, f"rollback {name} copy changed")
        need(protected_state_hash(STATE) == initial["protected_state_sha256"],
             "protected owner state changed; refusing to overwrite concurrent values")
        need(self.artifacts is not None, "rollback artifact authority is unavailable")
        need(tree_hash_excluding(CONFIG_DIR, {"service.json"})
             == tree_hash_excluding(rollback / "config", {"service.json"}),
             "owner config changed outside service.json")
        current_service = load_json(SERVICE_CONFIG)
        need(current_service in (load_json(self.artifacts["service_config_before"]),
                                 load_json(self.artifacts["service_config_after"])),
             "owner service config changed outside the reviewed keep-latest delta")
        expected_databases = set(initial["databases"])
        current_databases = {target.name for target in STATE.glob("*.sqlite")}
        preserved_unknown_databases = sorted(current_databases - expected_databases)
        inventory = initial["plists"]
        plist_hashes = initial["plist_sha256"]
        for label in LABELS:
            target = LAUNCH_AGENTS / f"{label}.plist"
            if target.exists():
                need(target.is_file() and not target.is_symlink()
                     and inventory[label] and sha(target) == plist_hashes[label],
                     f"LaunchAgent changed outside reviewed installer output: {label}")
            else:
                need(inventory[label] or not target.is_symlink(),
                     f"unexpected LaunchAgent path: {label}")
        restored_venv = RUNTIME / f".{rollback.name}.restore-venv"
        failed_venv = RUNTIME / f".{rollback.name}.failed-venv"
        shutil.copytree(rollback / "venv", restored_venv, symlinks=True)
        os.replace(VENV, failed_venv); os.replace(restored_venv, VENV)
        restored_config = DALTON / f".{rollback.name}.restore-config"
        failed_config = DALTON / f".{rollback.name}.failed-config"
        shutil.copytree(rollback / "config", restored_config, symlinks=True)
        os.replace(CONFIG_DIR, failed_config); os.replace(restored_config, CONFIG_DIR)
        manifest = load_json(rollback / "databases" / self.snapshot_id / "manifest.json")
        for row in manifest["files"]:
            need(isinstance(row.get("file"), str) and Path(row["file"]).name == row["file"]
                 and row["file"] in expected_databases, "rollback database inventory differs")
            target = STATE / row["file"]
            Path(f"{target}-wal").unlink(missing_ok=True); Path(f"{target}-shm").unlink(missing_ok=True)
            temporary = target.with_name(f".{target.name}.r11a-restore")
            shutil.copy2(rollback / "restore" / row["file"], temporary); os.replace(temporary, target)
        for label in LABELS:
            target = LAUNCH_AGENTS / f"{label}.plist"
            target.unlink(missing_ok=True)
            if inventory[label]: shutil.copy2(rollback / "plists" / target.name, target)
        need(tree_hash(VENV) == initial["backup_tree_hashes"]["venv"]
             and tree_hash(CONFIG_DIR) == initial["backup_tree_hashes"]["config"],
             "restored runtime or config bytes differ")
        self.restart_initial()
        shutil.rmtree(failed_venv); shutil.rmtree(failed_config)
        return {"status": "rolled_back_healthy", "bytes_restored": True,
                "service_config_sha256": sha(SERVICE_CONFIG), "model_configs": len(current_models()),
                "preserved_unknown_databases": preserved_unknown_databases}


def load_json_bytes(value: str) -> Any:
    try: return json.loads(value)
    except json.JSONDecodeError as exc: raise ExecuteError("command returned invalid JSON") from exc


def execute(packet: Path, expected_manifest_sha256: str, log_path: Path, receipt_path: Path,
            installed_verification_path: Path | None = None, *, do_execute: bool) -> dict[str, Any]:
    manifest_path = packet / "release-manifest.candidate.json"
    need(HEX64.fullmatch(expected_manifest_sha256) is not None
         and manifest_path.is_file() and sha(manifest_path) == expected_manifest_sha256,
         "explicit candidate manifest approval hash differs")
    manifest, artifacts = packet_preflight(packet)
    if not do_execute:
        with open(os.devnull, "w") as sink:
            preflight = Orchestrator(packet, sink).live_preflight(manifest, artifacts)
        preflight = dict(preflight); preflight["source"] = str(preflight["source"])
        return {"status": "reviewed_preflight_passed", "live_mutation": False, **preflight}
    need(installed_verification_path is not None, "execution requires an installed-verification output")
    need(not log_path.exists() and not receipt_path.exists() and not installed_verification_path.exists(),
         "execution output already exists")
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    started = datetime.now(timezone.utc).isoformat(); outcome: dict[str, Any]
    with os.fdopen(fd, "w", encoding="utf-8") as log:
        orchestrator = Orchestrator(packet, log)
        try:
            preflight = orchestrator.live_preflight(manifest, artifacts)
            orchestrator.disk_preflight(preflight["source"])
            orchestrator.stop_and_backup(preflight["source"])
            orchestrator.install(preflight["source"], artifacts)
            verified = orchestrator.verify_installed(artifacts)
            installed_record = {
                "schema_version": "r11a-installed-verification-0.1",
                "status": "installed_bytes_verified_runtime_pending",
                "source_commit": COMMIT,
                "verified_at": datetime.now(timezone.utc).isoformat(),
                "candidate_manifest_sha256": expected_manifest_sha256,
                "wheel_sha256": WHEEL_SHA256,
                "service_backup_keep_latest": load_json(SERVICE_CONFIG)["backup"]["keep_latest"],
                "model_config_count": verified["model_configs"],
                **verified,
            }
            exclusive_json(installed_verification_path, installed_record)
            outcome = {"status": "installer_finished_runtime_health_pending", "exit_code": 0,
                       "installed": verified,
                       "installed_verification": installed_verification_path.name,
                       "installed_verification_sha256": sha(installed_verification_path),
                       "rollback": {"status": "not_required"}}
        except (Exception, KeyboardInterrupt) as exc:
            orchestrator.note(f"FAIL {type(exc).__name__}: {exc}")
            try: recovery = orchestrator.rollback()
            except Exception as recovery_exc:
                recovery = {"status": "rollback_failed", "error": f"{type(recovery_exc).__name__}: {recovery_exc}"}
            outcome = {"status": "deployment_failed", "exit_code": 1,
                       "error": f"{type(exc).__name__}: {exc}", "rollback": recovery}
        if orchestrator.rollback_root is not None:
            outcome["fresh_rollback_snapshot"] = {
                "path": str(orchestrator.rollback_root),
                "database_snapshot_id": orchestrator.snapshot_id,
            }
    receipt = {"schema_version": "r11a-stopped-window-execution-0.1", "source_commit": COMMIT,
               "candidate_manifest_sha256": expected_manifest_sha256, "started_at": started,
               "finished_at": datetime.now(timezone.utc).isoformat(), "log": log_path.name,
               "log_sha256": sha(log_path), "manifest_modified": False, **outcome}
    exclusive_json(receipt_path, receipt); return receipt


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--log", type=Path)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--installed-verification", type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    need(not args.execute or (args.log is not None and args.receipt is not None
                              and args.installed_verification is not None),
         "--execute requires --log, --receipt and --installed-verification")
    result = execute(args.packet.resolve(), args.expected_manifest_sha256,
                     args.log.resolve() if args.log else Path("/nonexistent-log"),
                     args.receipt.resolve() if args.receipt else Path("/nonexistent-receipt"),
                     args.installed_verification.resolve() if args.installed_verification else None,
                     do_execute=args.execute)
    print(json.dumps(result)); return int(result.get("exit_code", 0))


if __name__ == "__main__":
    try: raise SystemExit(main())
    except (ExecuteError, OSError, subprocess.SubprocessError, zipfile.BadZipFile) as exc:
        raise SystemExit(f"STOP: {exc}") from exc
