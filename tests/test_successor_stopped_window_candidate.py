from __future__ import annotations

import io
import json
import tempfile
import unittest
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from scripts import execute_successor_stopped_window_candidate as execute
from scripts import finalize_successor_health_candidate as final
from scripts import observe_successor_health_candidate as health
from scripts.prepare_successor_config_transition import (
    DOCUMENT_CONFIG, LANE_CONFIG, MODEL_ADDITIONS, MODEL_REPLACEMENT,
)


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


class SuccessorStoppedWindowCandidateTests(unittest.TestCase):
    def test_template_is_inert_and_does_not_guess_release_identity(self) -> None:
        candidate = execute.template()
        self.assertEqual("inputs_incomplete", candidate["status"])
        self.assertEqual("pending", candidate["acceptance_state"])
        self.assertIsNone(candidate["source"]["commit"])
        self.assertIsNone(candidate["runtime"]["model_config_count_before"])
        self.assertIsNone(candidate["runtime"]["model_config_count_after"])
        self.assertEqual(execute.ARTIFACT_NAMES, set(candidate["artifacts"]))
        self.assertTrue(all(row == {"file": None, "sha256": None}
                            for row in candidate["artifacts"].values()))
        self.assertFalse(candidate["boundaries"]["manifest_publication"])

    def test_unaccepted_or_incomplete_packet_cannot_reach_live_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            packet = Path(temporary)
            manifest = execute.template()
            manifest["content_hash"] = execute.canonical_hash(manifest)
            write_json(packet / "release-manifest.candidate.json", manifest)
            with self.assertRaisesRegex(execute.SuccessorExecuteError,
                                        "candidate manifest differs"):
                execute.packet_preflight(packet)

    def test_rollback_restores_owned_targets_and_preserves_one_concurrent_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            packet = root / "packet"; packet.mkdir()
            state = root / "state"; state.mkdir()
            rollback = root / "rollback"; rollback.mkdir()
            rollback_state = rollback / "state-files"; rollback_state.mkdir()

            before = {"route": "before"}
            after = {"route": "after", "structured_output_repair": {"max_attempts": 1}}
            rows = []
            for name in (*MODEL_ADDITIONS, DOCUMENT_CONFIG, LANE_CONFIG):
                artifact = packet / (name + ".after")
                value = ({"schema_version": "0.1", "enabled": True}
                         if name == LANE_CONFIG else {"name": name})
                write_json(artifact, value)
                rows.append({"name": name, "kind": "exclusive_add",
                             "after": {"file": artifact.name,
                                       "sha256": execute.sha(artifact)}})
                write_json(state / name, value)
            before_path = packet / "initial.before.json"
            after_path = packet / "initial.after.json"
            write_json(before_path, before); write_json(after_path, after)
            rows.insert(2, {"name": MODEL_REPLACEMENT,
                            "kind": "compare_and_replace",
                            "before": {"file": before_path.name,
                                       "sha256": execute.sha(before_path)},
                            "after": {"file": after_path.name,
                                      "sha256": execute.sha(after_path)}})
            write_json(state / MODEL_REPLACEMENT, after)
            write_json(rollback_state / MODEL_REPLACEMENT, before)
            write_json(state / "owner.json", {"kept": True})
            write_json(rollback_state / "owner.json", {"kept": True})
            transition = packet / "transition.json"
            write_json(transition, {"targets": rows})
            write_json(rollback / "initial-state.json", {
                "protected_state_sha256": "old",
            })

            # A concurrent owner replaces only one successor addition.  The
            # rollback must preserve it while restoring every other owned byte.
            conflict = state / MODEL_ADDITIONS[0]
            write_json(conflict, {"concurrent": True})
            worker = execute.SuccessorOrchestrator(packet, io.StringIO())
            worker.stopped = worker.mutations_started = True
            worker.rollback_root = rollback
            worker.artifacts = {"transition_manifest": transition}
            with patch.object(execute.r11.Orchestrator, "rollback",
                              return_value={"status": "rolled_back_healthy"}) as parent:
                with patch.object(execute.r11, "STATE", state):
                    result = worker.rollback()

            parent.assert_called_once_with()
            self.assertEqual([MODEL_ADDITIONS[0]],
                             result["preserved_concurrent_config_targets"])
            self.assertEqual({"concurrent": True}, json.loads(conflict.read_text()))
            self.assertEqual(before, json.loads((state / MODEL_REPLACEMENT).read_text()))
            self.assertFalse((state / MODEL_ADDITIONS[1]).exists())
            self.assertFalse((state / DOCUMENT_CONFIG).exists())
            self.assertFalse((state / LANE_CONFIG).exists())
            initial = json.loads((rollback / "initial-state.json").read_text())
            self.assertEqual(execute.r11.protected_state_hash(state),
                             initial["protected_state_sha256"])

    def test_rollback_refuses_unrelated_owner_drift_before_parent_restore(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            packet = root / "packet"; packet.mkdir()
            state = root / "state"; state.mkdir()
            rollback = root / "rollback"; rollback.mkdir()
            rollback_state = rollback / "state-files"; rollback_state.mkdir()
            artifact = packet / "draft.after.json"; write_json(artifact, {"draft": True})
            transition = packet / "transition.json"
            write_json(transition, {"targets": [{
                "name": MODEL_ADDITIONS[0], "kind": "exclusive_add",
                "after": {"file": artifact.name, "sha256": execute.sha(artifact)},
            }]})
            write_json(state / MODEL_ADDITIONS[0], {"concurrent": True})
            write_json(state / "owner.json", {"owner": "changed"})
            write_json(rollback_state / "owner.json", {"owner": "before"})
            write_json(rollback / "initial-state.json", {"protected_state_sha256": "old"})
            worker = execute.SuccessorOrchestrator(packet, io.StringIO())
            worker.stopped = worker.mutations_started = True
            worker.rollback_root = rollback
            worker.artifacts = {"transition_manifest": transition}
            with patch.object(execute.r11.Orchestrator, "rollback") as parent:
                with patch.object(execute.r11, "STATE", state):
                    with self.assertRaisesRegex(execute.SuccessorExecuteError,
                                                "outside successor"):
                        worker.rollback()
            parent.assert_not_called()
            self.assertEqual({"concurrent": True},
                             json.loads((state / MODEL_ADDITIONS[0]).read_text()))

    def test_health_observation_uses_manifest_bound_window(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); packet = root / "packet"; packet.mkdir()
            manifest_path = packet / "release-manifest.candidate.json"
            manifest = {"source": {"commit": "a" * 40},
                        "health_acceptance": {"sample_count": 3,
                                              "interval_seconds": 5,
                                              "minimum_duration_seconds": 10}}
            write_json(manifest_path, manifest)
            started = datetime.now(timezone.utc) - timedelta(seconds=2)
            controller_started = (started + timedelta(seconds=1)).isoformat()
            deployment_path = packet / "deployment.json"
            write_json(deployment_path, {
                "schema_version": "successor-stopped-window-execution-0.1",
                "source_commit": "a" * 40,
                "status": "installer_finished_runtime_health_pending", "exit_code": 0,
                "candidate_manifest_sha256": execute.sha(manifest_path),
                "started_at": started.isoformat(),
                "finished_at": datetime.now(timezone.utc).isoformat(),
            })
            clock = [0.0]
            def sleep(seconds): clock[0] += seconds
            def invoke(*_args, **_kwargs):
                wire = {"ok": True,
                        "checks": {"writer": True, "heartbeat_age_seconds": .1},
                        "heartbeat": {"pid": 42, "started_at": controller_started,
                                      "last_tick_at": controller_started}}
                return subprocess.CompletedProcess([], 0, json.dumps(wire), "")
            with patch.object(health, "packet_preflight", return_value=(manifest, {})):
                result = health.observe(
                    packet, deployment_path, root / "health", ["health"],
                    invoke=invoke, sleep=sleep, clock=lambda: clock[0])
            self.assertTrue(result["accepted"])
            self.assertEqual(["0000.json", "0001.json", "0002.json"],
                             [row["raw_file"] for row in result["samples"]])

    def test_finalizer_rechecks_runtime_and_never_publishes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); packet = root / "packet"; packet.mkdir()
            source = root / "source"; source.mkdir()
            manifest_path = packet / "release-manifest.candidate.json"
            policy = {"sample_count": 3, "interval_seconds": 5,
                      "minimum_duration_seconds": 10}
            manifest = {
                "source": {"root": str(source), "commit": "b" * 40},
                "acceptance": {"wheel_sha256": "c" * 64},
                "runtime": {"model_config_count_after": 17},
                "health_acceptance": policy,
            }
            write_json(manifest_path, manifest)
            installed_path = packet / "installed.json"
            exact = {"model_config_count": 17, "runtime_files": 9,
                     "authority": {"mission": "exact"},
                     "writer_lane_enabled": True, "thesis_impact_enabled": False,
                     "backup_keep_latest": 3}
            write_json(installed_path, {
                "schema_version": "successor-installed-verification-0.1",
                "status": "installed_bytes_verified_runtime_pending",
                "source_commit": "b" * 40,
                "candidate_manifest_sha256": execute.sha(manifest_path),
                "wheel_sha256": "c" * 64, **exact,
            })
            deployed_start = datetime(2026, 9, 11, tzinfo=timezone.utc)
            deployed_finish = deployed_start + timedelta(seconds=2)
            rollback = packet / "rollback"; rollback.mkdir()
            write_json(rollback / "initial-state.json", {"loaded": ["writer"]})
            deployment_path = packet / "deployment.json"
            write_json(deployment_path, {
                "schema_version": "successor-stopped-window-execution-0.1",
                "source_commit": "b" * 40,
                "status": "installer_finished_runtime_health_pending", "exit_code": 0,
                "candidate_manifest_sha256": execute.sha(manifest_path),
                "installed_verification": installed_path.name,
                "installed_verification_sha256": execute.sha(installed_path),
                "started_at": deployed_start.isoformat(),
                "finished_at": deployed_finish.isoformat(),
                "fresh_rollback_snapshot": {"path": str(rollback)},
            })
            health_dir = packet / "health"; health_dir.mkdir()
            samples = []
            controller_started = (deployed_start + timedelta(seconds=1)).isoformat()
            for index in range(3):
                at = (deployed_finish + timedelta(seconds=index * 5)).isoformat()
                wire = {"ok": True,
                        "checks": {"writer": True, "heartbeat_age_seconds": .1},
                        "heartbeat": {"pid": 42, "started_at": controller_started}}
                raw = health_dir / f"{index:04d}.json"
                write_json(raw, {"at": at, "exit_code": 0,
                                 "stdout": json.dumps(wire), "stderr": ""})
                samples.append({"at": at, "exit_code": 0, "ok": True,
                                "checks": wire["checks"], "pid": 42,
                                "started_at": controller_started,
                                "raw_file": raw.name, "raw_sha256": execute.sha(raw)})
            summary_path = health_dir / "summary.json"
            write_json(summary_path, {
                "schema_version": "successor-health-observation-0.1", "status": "passed",
                "source_commit": "b" * 40,
                "candidate_manifest_sha256": execute.sha(manifest_path),
                "deployment_receipt_sha256": execute.sha(deployment_path),
                "health_acceptance": policy, "sample_count": 3, "elapsed_seconds": 10,
                "all_healthy": True, "same_controller": True,
                "postdeployment_controller": True, "observed_long_enough": True,
                "accepted": True, "samples": samples,
            })
            class FakeWorker:
                def __init__(self, *_args): pass
                def verify_successor(self, _manifest, _artifacts): return exact
            with patch.object(final, "packet_preflight", return_value=(manifest, {})), \
                 patch.object(final, "SuccessorOrchestrator", FakeWorker), \
                 patch.object(final.subprocess, "check_output",
                              side_effect=["b" * 40 + "\n", ""]):
                result = final.finalize(
                    packet, deployment_path, summary_path, installed_path,
                    packet / "runtime-verification.json", packet / "post.log")
            self.assertEqual("passed_pending_publication", result["status"])
            self.assertFalse(result["manifest_publication"])
            self.assertFalse((packet / "current-release.json").exists())


if __name__ == "__main__":
    unittest.main()
