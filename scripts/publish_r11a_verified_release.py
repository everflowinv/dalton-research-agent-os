#!/usr/bin/env python3
"""Publish the current release pointer after fresh R11a runtime verification.

This records deployment acceptance under the owner's existing deployment
authorization. It does not sign research or governance decisions, and keeps
the legacy deployment manifest unchanged as historical evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_module(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def publish(packet: Path, owner: Path, expected_manifest: str,
            host_transition: Path | None = None, expected_transition: str | None = None) -> dict:
    manifest_path = packet / "release-manifest.candidate.json"
    if sha(manifest_path) != expected_manifest:
        raise ValueError("reviewed manifest changed")
    current = owner / "current-release.json"
    if current.exists() or current.is_symlink():
        raise ValueError("current release pointer already exists; review its successor explicitly")
    legacy = owner / "release-manifest.json"
    legacy_hash = sha(legacy)
    old_launcher = owner / "deploy-wave4.zsh"
    launcher_before = old_launcher.read_bytes()
    launcher_hash = sha(old_launcher)
    guard = b'''# Refuse an obsolete deployment before stopping any service.
if [[ -e "$packet_dir/current-release.json" ]]; then
  print -u2 'STOP: this historical installer is superseded; see current-release.json for the accepted release.'
  exit 1
fi

'''
    anchor = b"packet_dir=${0:A:h}\n"
    if launcher_before.count(anchor) != 1:
        raise ValueError("historical launcher identity is unsupported")
    launcher_after = launcher_before.replace(anchor, anchor + guard)
    reviewed = json.loads(manifest_path.read_text())
    for name in ("execute_r11a_stopped_window_candidate.py", "finalize_r11a_health_candidate.py"):
        if sha(packet / name) != reviewed["helpers"][name]:
            raise ValueError("reviewed verification helper changed")
    execute = load_module(packet / "execute_r11a_stopped_window_candidate.py")
    final = load_module(packet / "finalize_r11a_health_candidate.py")
    manifest, artifacts = execute.packet_preflight(packet)
    effective_artifacts = dict(artifacts)
    transition = None
    if host_transition is not None:
        if not expected_transition or sha(host_transition) != expected_transition:
            raise ValueError("reviewed host transition hash differs")
        transition = json.loads(host_transition.read_text())
        if transition.get("schema_version") != "r11a-host-skill-transition-0.1":
            raise ValueError("unsupported host transition review")
        before = artifacts["openclaw_config_snapshot"]
        for field in ("config_file", "plugin_snapshot_file"):
            if Path(transition[field]).name != transition[field]:
                raise ValueError("host transition path is not packet-relative")
        after = host_transition.parent / transition["config_file"]
        plugin = host_transition.parent / transition["plugin_snapshot_file"]
        if sha(before) != transition["before_sha256"] or sha(after) != transition["after_sha256"] \
                or sha(plugin) != transition["plugin_snapshot_sha256"]:
            raise ValueError("host transition bytes differ")
        a, b = json.loads(before.read_text()), json.loads(after.read_text())
        if {k: v for k, v in a.items() if k != "skills"} != {k: v for k, v in b.items() if k != "skills"}:
            raise ValueError("host transition changes non-skill configuration")
        old_plugin = json.loads(artifacts["provider_plugin_snapshot"].read_text())
        new_plugin = json.loads(plugin.read_text())
        if {k: v for k, v in old_plugin.items() if k != "openclaw_config_sha256"} != \
                {k: v for k, v in new_plugin.items() if k != "openclaw_config_sha256"}:
            raise ValueError("host transition changes provider plugin authority")
        effective_artifacts.update(openclaw_config_snapshot=after, provider_plugin_snapshot=plugin)
    deployment_path = packet / "r11a-deploy-receipt.json"
    deployment = json.loads(deployment_path.read_text())
    rollback = Path(deployment["fresh_rollback_snapshot"]["path"])
    initial = json.loads((rollback / "initial-state.json").read_text())
    health = packet / "health-observation-v2/summary.json"
    with tempfile.TemporaryDirectory(prefix=".publish-check-", dir=packet) as temporary:
        check = Path(temporary)
        verified = final.finalize(
            manifest_path, deployment_path, health,
            packet / "installed-verification.json", artifacts["wheel"],
            next((execute.VENV / "lib").glob("python*/site-packages")),
            check / "runtime-verification.json",
        )
        with (check / "post-observation.log").open("w") as log:
            orchestrator = execute.Orchestrator(packet, log)
            orchestrator.rollback_root = rollback
            orchestrator.initially_loaded = initial["loaded"]
            live = orchestrator.verify_installed(effective_artifacts)
        if sha(manifest_path) != expected_manifest or sha(legacy) != legacy_hash:
            raise ValueError("release authority changed during publication checks")
        if old_launcher.read_bytes() != launcher_before:
            raise ValueError("historical launcher changed during publication checks")
        record = {
            "schema_version": "dalton-current-release-0.1",
            "status": "deployed_verified",
            "source_commit": execute.COMMIT,
            "release_ref": manifest["release_ref"],
            "release_packet": str(packet),
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "runtime_health_only": True,
            "research_completion_claimed": False,
            "research_governance_signed": False,
            "deployment_authorization": "owner previously authorized autonomous deployment",
            "candidate_manifest_sha256": expected_manifest,
            "deployment_receipt_sha256": sha(deployment_path),
            "health_summary_sha256": sha(health),
            "runtime_verification": verified,
            "post_observation_live_verification": live,
            "historical_manifest": str(legacy),
            "historical_manifest_sha256": legacy_hash,
            "historical_launcher_before_sha256": launcher_hash,
            "historical_launcher_after_sha256": hashlib.sha256(launcher_after).hexdigest(),
            "publisher_sha256": sha(Path(__file__)),
            "post_install_host_transition": transition,
            "post_install_host_transition_sha256": expected_transition,
        }
        # Publish complete bytes atomically, without replacing a concurrent pointer.
        pointer_draft = check / "current-release.json"
        pointer_fd = os.open(pointer_draft, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(pointer_fd, "w") as stream:
            json.dump(record, stream, ensure_ascii=False, indent=2)
            stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
        os.link(pointer_draft, current)
        # Keep a byte proof, not another database/runtime backup.
        execute.exclusive_json(packet / "publication-receipt.json", record)
        if old_launcher.read_bytes() != launcher_before:
            raise ValueError("pointer published; concurrent launcher change preserved")
        replacement = owner / ".deploy-wave4.r11a-guard.zsh"
        fd = os.open(replacement, os.O_WRONLY | os.O_CREAT | os.O_EXCL, old_launcher.stat().st_mode & 0o777)
        with os.fdopen(fd, "wb") as stream:
            stream.write(launcher_after); stream.flush(); os.fsync(stream.fileno())
        os.replace(replacement, old_launcher)
        for name in ("runtime-verification.json", "post-observation.log"):
            target = packet / ("published-" + name)
            with target.open("xb") as stream:
                stream.write((check / name).read_bytes())
            os.chmod(target, 0o600)
        return record


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--owner-packet", type=Path, required=True)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--host-transition-review", type=Path)
    parser.add_argument("--expected-host-transition-sha256")
    args = parser.parse_args()
    result = publish(args.packet.resolve(), args.owner_packet.resolve(), args.expected_manifest_sha256,
                     args.host_transition_review, args.expected_host_transition_sha256)
    print(json.dumps({"status": result["status"], "source_commit": result["source_commit"]}))
