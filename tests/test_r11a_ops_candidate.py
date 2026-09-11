from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import tempfile
import unittest
import zipfile
import io
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


class R11aOpsCandidateTests(unittest.TestCase):
    def test_shipped_inputs_are_inert_and_have_no_live_boundary(self) -> None:
        stage = load("stage_r11a_ops_candidate")
        template = json.loads((ROOT / "deploy/release/r11a-candidate/inputs.template.json").read_text())
        self.assertEqual("incomplete", template["status"])
        self.assertEqual(stage.BOUNDARIES, template["boundaries"])
        self.assertTrue(any(row["path"] is None or row["sha256"] is None
                            for row in template["artifacts"].values()))

    def test_health_acceptance_requires_one_postdeployment_controller_and_full_duration(self) -> None:
        health = load("observe_r11a_health_candidate")
        start = datetime(2026, 9, 11, tzinfo=timezone.utc)
        finished = start + timedelta(seconds=20)
        controller = (start + timedelta(seconds=10)).isoformat()
        samples = [{"exit_code": 0, "ok": True,
                    "checks": {"writer": True, "heartbeat_age_seconds": 0.2},
                    "pid": 123, "started_at": controller}
                   for _ in range(health.SAMPLE_COUNT)]
        self.assertTrue(health.acceptance(samples, health.MIN_OBSERVATION_SECONDS,
                                          start, finished)["accepted"])
        samples[-1]["pid"] = 124
        self.assertFalse(health.acceptance(samples, health.MIN_OBSERVATION_SECONDS,
                                           start, finished)["accepted"])

    def test_finalizer_writes_candidate_without_publishing_manifest(self) -> None:
        final = load("finalize_r11a_health_candidate")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); installed_root = root / "site"; (installed_root / "dalton_core").mkdir(parents=True)
            body = b"VALUE = 1\n"; wheel = root / "accepted.whl"
            with zipfile.ZipFile(wheel, "w") as archive: archive.writestr("dalton_core/example.py", body)
            (installed_root / "dalton_core/example.py").write_bytes(body)
            final.WHEEL_SHA256 = hashlib.sha256(wheel.read_bytes()).hexdigest()
            manifest_path = root / "approved.json"
            write_json(manifest_path, {"source": {"commit": final.COMMIT},
                                      "status": "staged_pending_owner_acceptance"})
            deployment_path = root / "deployment.json"
            deploy_start = datetime(2026, 9, 11, tzinfo=timezone.utc)
            deploy_finish = deploy_start + timedelta(seconds=10)
            write_json(deployment_path, {"source_commit": final.COMMIT,
                                         "status": "installer_finished_runtime_health_pending",
                                         "exit_code": 0,
                                         "started_at": deploy_start.isoformat(),
                                         "finished_at": deploy_finish.isoformat(),
                                         "candidate_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest()})
            summary_path = root / "summary.json"
            controller_started = (deploy_start + timedelta(seconds=5)).isoformat()
            samples = []
            for index in range(45):
                at = (deploy_finish + timedelta(seconds=index * 15)).isoformat()
                wire = {"ok": True, "checks": {"writer": True, "heartbeat_age_seconds": 0.1},
                        "heartbeat": {"pid": 321, "started_at": controller_started}}
                raw_path = root / f"{index:02d}.json"
                write_json(raw_path, {"at": at, "exit_code": 0,
                                      "stdout": json.dumps(wire), "stderr": ""})
                samples.append({"at": at, "exit_code": 0, "ok": True, "checks": wire["checks"],
                                "pid": 321, "started_at": controller_started,
                                "raw_file": raw_path.name,
                                "raw_sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest()})
            write_json(summary_path, {"source_commit": final.COMMIT, "accepted": True,
                                      "all_healthy": True, "same_controller": True,
                                      "postdeployment_controller": True, "observed_long_enough": True,
                                      "sample_count": 45, "elapsed_seconds": 660, "samples": samples,
                                      "candidate_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                                      "deployment_receipt_sha256": hashlib.sha256(deployment_path.read_bytes()).hexdigest()})
            installed_path = root / "installed.json"
            write_json(installed_path, {"status": "installed_bytes_verified_runtime_pending",
                                        "source_commit": final.COMMIT, "wheel_sha256": final.WHEEL_SHA256,
                                        "service_backup_keep_latest": 3, "model_config_count": 15})
            before = manifest_path.read_bytes(); output = root / "candidate.json"
            receipt = final.finalize(manifest_path, deployment_path, summary_path,
                                     installed_path, wheel, installed_root, output)
            self.assertEqual("passed_pending_owner_publication", receipt["status"])
            self.assertFalse(receipt["manifest_publication"])
            self.assertEqual(before, manifest_path.read_bytes())

    def test_stopped_window_rollback_restores_runtime_config_state_database_and_plists(self) -> None:
        execute = load("execute_r11a_stopped_window_candidate")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            execute.DALTON = root / "Dalton"; execute.STATE = execute.DALTON / "state/dalton-core"
            execute.CONFIG_DIR = execute.DALTON / "config"; execute.SERVICE_CONFIG = execute.CONFIG_DIR / "service.json"
            execute.RUNTIME = execute.DALTON / "runtime"; execute.VENV = execute.RUNTIME / "venv"
            execute.LAUNCH_AGENTS = root / "LaunchAgents"
            execute.STATE.mkdir(parents=True); execute.CONFIG_DIR.mkdir(); execute.VENV.mkdir(parents=True)
            execute.LAUNCH_AGENTS.mkdir()
            (execute.VENV / "new.txt").write_text("new")
            write_json(execute.SERVICE_CONFIG, {"version": "new"})
            write_json(execute.STATE / "owned.json", {"version": "old"})
            (execute.STATE / "authority.sqlite").write_bytes(b"new-database")
            (execute.STATE / "discovery-plans").mkdir(); (execute.STATE / "discovery-plans/old.json").write_text("old")
            writer = execute.LABELS[0]
            (execute.LAUNCH_AGENTS / f"{writer}.plist").write_bytes(b"new-plist")
            rollback = root / "rollback"; rollback.mkdir()
            (rollback / "venv").mkdir(); (rollback / "venv/old.txt").write_text("old")
            (rollback / "config").mkdir(); write_json(rollback / "config/service.json", {"version": "old"})
            state_files = rollback / "state-files"; state_files.mkdir(); write_json(state_files / "owned.json", {"version": "old"})
            (state_files / "discovery-plans").mkdir(); (state_files / "discovery-plans/old.json").write_text("old")
            (rollback / "plists").mkdir(); (rollback / f"plists/{writer}.plist").write_bytes(b"old-plist")
            write_json(rollback / "initial-state.json", {"loaded": [writer],
                "plists": {label: label == writer for label in execute.LABELS},
                "databases": ["authority.sqlite"],
                "backup_tree_hashes": {
                    "venv": execute.tree_hash(rollback / "venv"),
                    "config": execute.tree_hash(rollback / "config"),
                    "state-files": execute.tree_hash(state_files),
                    "plists": execute.tree_hash(rollback / "plists"),
                }})
            snapshot = "snapshot"; (rollback / f"databases/{snapshot}").mkdir(parents=True)
            write_json(rollback / f"databases/{snapshot}/manifest.json",
                       {"files": [{"file": "authority.sqlite"}]})
            (rollback / "restore").mkdir(); (rollback / "restore/authority.sqlite").write_bytes(b"old-database")
            class Fake(execute.Orchestrator):
                def __init__(self):
                    super().__init__(root, io.StringIO()); self.restarted = False
                def stop(self, _label): pass
                def restart_initial(self): self.restarted = True
            worker = Fake(); worker.stopped = worker.mutations_started = True
            artifacts = root / "artifacts"; artifacts.mkdir()
            write_json(artifacts / "service-after.json", {"version": "new"})
            worker.rollback_root = rollback; worker.snapshot_id = snapshot; worker.initially_loaded = [writer]
            worker.source = root; worker.artifacts = {"service_config_after": artifacts / "service-after.json"}
            # The physical rollback test bypasses only process drain commands.
            worker.command = lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "", "")
            write_json(execute.STATE / "owned.json", {"version": "concurrent-owner"})
            with self.assertRaisesRegex(execute.ExecuteError, "refusing to overwrite concurrent"):
                worker.rollback()
            self.assertTrue((execute.VENV / "new.txt").is_file())
            self.assertEqual({"version": "concurrent-owner"},
                             json.loads((execute.STATE / "owned.json").read_text()))
            write_json(execute.STATE / "owned.json", {"version": "old"})
            result = worker.rollback()
            self.assertEqual("rolled_back_healthy", result["status"])
            self.assertTrue(worker.restarted)
            self.assertTrue((execute.VENV / "old.txt").is_file())
            self.assertEqual({"version": "old"}, json.loads(execute.SERVICE_CONFIG.read_text()))
            self.assertEqual(b"old-database", (execute.STATE / "authority.sqlite").read_bytes())
            self.assertTrue((execute.STATE / "discovery-plans/old.json").is_file())
            self.assertEqual(b"old-plist", (execute.LAUNCH_AGENTS / f"{writer}.plist").read_bytes())
            self.assertFalse((execute.LAUNCH_AGENTS / f"{execute.LABELS[-1]}.plist").exists())


if __name__ == "__main__":
    unittest.main()
