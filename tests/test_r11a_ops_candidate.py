from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import tempfile
import unittest
import zipfile
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

    def _approved_plan(self, root: Path):
        deploy = load("run_r11a_deploy_candidate")
        wheel = root / "accepted.whl"; wheel.write_bytes(b"wheel")
        deploy.WHEEL_SHA256 = hashlib.sha256(wheel.read_bytes()).hexdigest()
        steps = []
        for name in deploy.ORDER:
            program_name = "zsh" if name == "frozen_installer" else f"{name}.py"
            program = root / program_name
            program.write_text("#!/bin/sh\nexit 0\n")
            argv = [str(program)]
            if name == "preinstall_wheel": argv += ["--no-deps", "--force-reinstall", str(wheel)]
            if name == "configure_retention": argv += ["--service-config", "/config", "--expected-before-sha256", "a" * 64]
            if name == "frozen_installer": argv += [str(root / "install.sh")]
            steps.append({"name": name, "argv": argv,
                          "program_sha256": hashlib.sha256(program.read_bytes()).hexdigest()})
        manifest = {"schema_version": deploy.SCHEMA, "release_ref": "foundation-followup-r11a",
                    "source_commit": deploy.COMMIT, "acceptance_state": "passed",
                    "deployment_state": "not_started", "wheel_file": wheel.name,
                    "wheel_sha256": deploy.WHEEL_SHA256, "deployment_steps": steps}
        manifest["content_hash"] = deploy.canonical_hash(manifest)
        path = root / "release-manifest.approved.json"; write_json(path, manifest)
        return deploy, path

    def test_deploy_review_is_inert_and_execution_order_is_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); deploy, manifest = self._approved_plan(root)
            reviewed = deploy.run(manifest, root / "unused.json", execute=False)
            self.assertEqual("approved_plan_verified_inert", reviewed["status"])
            self.assertFalse(reviewed["live_mutation"])
            calls = []
            def invoke(argv, **_kwargs):
                calls.append(Path(argv[0]).name)
                return subprocess.CompletedProcess(argv, 0, "ok", "")
            receipt_path = root / "execution.json"
            receipt = deploy.run(manifest, receipt_path, execute=True, invoke=invoke)
            self.assertEqual("installer_finished", receipt["status"])
            self.assertEqual(list(deploy.ORDER), [row["name"] for row in receipt["steps"]])
            self.assertEqual(7, len(calls))
            self.assertTrue(receipt_path.is_file())
            self.assertEqual("not_started", json.loads(manifest.read_text())["deployment_state"])

    def test_deploy_stops_after_first_failed_step(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); deploy, manifest = self._approved_plan(root)
            calls = []
            def invoke(argv, **_kwargs):
                calls.append(argv)
                code = 9 if len(calls) == 3 else 0
                return subprocess.CompletedProcess(argv, code, "", "failed" if code else "")
            receipt = deploy.run(manifest, root / "execution.json", execute=True, invoke=invoke)
            self.assertEqual("deployment_step_failed", receipt["status"])
            self.assertEqual(9, receipt["exit_code"])
            self.assertEqual(3, len(calls))

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
            write_json(manifest_path, {"source_commit": final.COMMIT, "acceptance_state": "passed"})
            deployment_path = root / "deployment.json"
            deploy_start = datetime(2026, 9, 11, tzinfo=timezone.utc)
            deploy_finish = deploy_start + timedelta(seconds=10)
            write_json(deployment_path, {"source_commit": final.COMMIT, "status": "installer_finished",
                                         "exit_code": 0,
                                         "started_at": deploy_start.isoformat(),
                                         "finished_at": deploy_finish.isoformat(),
                                         "approved_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest()})
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
                                      "approved_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
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


if __name__ == "__main__":
    unittest.main()
