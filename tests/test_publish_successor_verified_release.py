from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import publish_successor_verified_release as publish


def write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


class SuccessorPublisherTests(unittest.TestCase):
    def fixture(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        root = Path(temporary.name); packet = root / "packet"; owner = root / "owner"
        state = root / "state"; packet.mkdir(); owner.mkdir(); state.mkdir()
        manifest = {
            "source": {"commit": "b" * 40}, "release_ref": "directed-originals-r12a",
            "runtime": {"model_config_count_after": 17},
        }
        manifest_path = packet / "release-manifest.candidate.json"; write(manifest_path, manifest)
        transition_path = packet / "transition.json"; write(transition_path, {"targets": []})
        after = {}
        for index in range(17):
            name = f"role-{index:02d}-model-config.json"
            value = {"purpose": f"role-{index:02d}"}; after[name] = value; write(state / name, value)
        after_path = packet / "models.after.json"; write(after_path, after)
        document = {"policy": "active"}; lane = {"schema_version": "0.1", "enabled": True}
        write(state / publish.DOCUMENT_CONFIG, document); write(state / publish.LANE_CONFIG, lane)
        deployment = {
            "status": "installer_finished_runtime_health_pending", "exit_code": 0,
            "source_commit": "b" * 40, "candidate_manifest_sha256": publish.sha(manifest_path),
            "fresh_rollback_snapshot": {"path": str(packet / "rollback"),
                                        "database_snapshot_id": "snapshot-1"},
        }
        rollback = packet / "rollback"; rollback.mkdir(); write(rollback / "initial-state.json", {})
        deploy_path = packet / "deploy.json"; write(deploy_path, deployment)
        health_path = packet / "health.json"; write(health_path, {"status": "passed"})
        installed_path = packet / "installed.json"; write(installed_path, {"status": "passed"})
        finalization = {
            "schema_version": "successor-runtime-verification-candidate-0.1",
            "status": "passed_pending_publication", "source_commit": "b" * 40,
            "verified_at": "2026-09-11T16:00:00+00:00",
            "candidate_manifest_sha256": publish.sha(manifest_path),
            "deployment_receipt_sha256": publish.sha(deploy_path),
            "health_summary_sha256": publish.sha(health_path),
            "installed_verification_sha256": publish.sha(installed_path),
            "runtime_verification": {"authority": "exact"},
            "manifest_publication": False,
        }
        final_path = packet / "final.json"; write(final_path, finalization)
        previous_release = {"status": "deployed_verified", "source_commit": "a" * 40,
                            "release_ref": "foundation-followup-r11a"}
        current_release = owner / "current-release.json"; write(current_release, previous_release)
        previous_runtime = {"schema_version": "dalton-runtime-config-pointer-0.1",
                            "base_release_commit": "a" * 40,
                            "base_release_pointer_sha256": publish.sha(current_release)}
        current_runtime = owner / "current-runtime-config.json"; write(current_runtime, previous_runtime)
        artifacts = {"transition_manifest": transition_path,
                     "model_config_after_snapshot": after_path}
        args = dict(
            packet=packet, owner=owner,
            expected_manifest_sha256=publish.sha(manifest_path),
            deployment_path=deploy_path, expected_deployment_sha256=publish.sha(deploy_path),
            health_path=health_path, expected_health_sha256=publish.sha(health_path),
            installed_path=installed_path, expected_installed_sha256=publish.sha(installed_path),
            finalization_path=final_path, expected_finalization_sha256=publish.sha(final_path),
            expected_current_release_sha256=publish.sha(current_release),
            expected_current_runtime_config_sha256=publish.sha(current_runtime),
            receipt_path=packet / "publication-receipt.json",
        )
        return packet, owner, state, manifest, artifacts, finalization, args

    def run_publish(self, *, fault_hook=None):
        packet, owner, state, manifest, artifacts, accepted, args = self.fixture()
        with patch.object(publish.execute, "packet_preflight",
                          return_value=(manifest, artifacts)), \
             patch.object(publish.finalizer, "finalize",
                          return_value=accepted), \
             patch.object(publish, "expected_transition_state",
                          return_value=(json.loads((packet / "models.after.json").read_text()),
                                        {"policy": "active"},
                                        {"schema_version": "0.1", "enabled": True})), \
             patch.object(publish.execute.r11, "STATE", state), \
             patch.object(publish.execute.r11, "current_models",
                          return_value=json.loads((packet / "models.after.json").read_text())):
            result = publish.publish(**args, fault_hook=fault_hook)
        return packet, owner, result, args

    def test_publishes_exact_successor_and_retains_r11a_predecessor(self):
        packet, owner, result, args = self.run_publish()
        current = json.loads((owner / "current-release.json").read_text())
        runtime = json.loads((owner / "current-runtime-config.json").read_text())
        self.assertEqual(result["status"], "published_verified")
        self.assertEqual(current["previous_release"]["release_ref"],
                         "foundation-followup-r11a")
        self.assertEqual(current["fresh_rollback_snapshot"]["database_snapshot_id"],
                         "snapshot-1")
        self.assertEqual(len(runtime["model_configs"]), 17)
        self.assertEqual(current["current_runtime_config_sha256"],
                         publish.sha(owner / "current-runtime-config.json"))
        self.assertFalse(current["research_governance_signed"])
        self.assertEqual((packet / "previous-current-release.json").read_bytes(),
                         json.dumps({"status": "deployed_verified", "source_commit": "a" * 40,
                                     "release_ref": "foundation-followup-r11a"},
                                    indent=2).encode() + b"\n")

    def test_stale_current_pointer_rejects_without_writes(self):
        packet, owner, state, manifest, artifacts, accepted, args = self.fixture()
        before = (owner / "current-runtime-config.json").read_bytes()
        args["expected_current_release_sha256"] = "0" * 64
        with patch.object(publish.execute, "packet_preflight",
                          return_value=(manifest, artifacts)):
            with self.assertRaisesRegex(publish.SuccessorPublicationError,
                                        "current release pointer changed"):
                publish.publish(**args)
        self.assertEqual((owner / "current-runtime-config.json").read_bytes(), before)
        self.assertFalse(args["receipt_path"].exists())

    def test_concurrent_release_edit_rolls_back_owned_runtime_pointer(self):
        changed = {"owner": "concurrent"}
        holder = {}
        def fault(seam):
            if seam == "after_runtime_pointer":
                write(holder["owner"] / "current-release.json", changed)
        packet, owner, state, manifest, artifacts, accepted, args = self.fixture()
        holder["owner"] = owner
        runtime_before = (owner / "current-runtime-config.json").read_bytes()
        with patch.object(publish.execute, "packet_preflight",
                          return_value=(manifest, artifacts)), \
             patch.object(publish.finalizer, "finalize", return_value=accepted), \
             patch.object(publish, "expected_transition_state",
                          return_value=(json.loads((packet / "models.after.json").read_text()),
                                        {"policy": "active"},
                                        {"schema_version": "0.1", "enabled": True})), \
             patch.object(publish.execute.r11, "STATE", state), \
             patch.object(publish.execute.r11, "current_models",
                          return_value=json.loads((packet / "models.after.json").read_text())):
            with self.assertRaisesRegex(publish.SuccessorPublicationError,
                                        "current release changed during publication"):
                publish.publish(**args, fault_hook=fault)
        self.assertEqual(json.loads((owner / "current-release.json").read_text()), changed)
        self.assertEqual((owner / "current-runtime-config.json").read_bytes(), runtime_before)
        self.assertFalse(args["receipt_path"].exists())

    def test_retry_recovers_exact_half_publication_and_finishes(self):
        packet, owner, state, manifest, artifacts, accepted, args = self.fixture()
        def crash(seam):
            if seam == "after_runtime_pointer":
                raise KeyboardInterrupt("simulated process loss")
        common = (
            patch.object(publish.execute, "packet_preflight",
                         return_value=(manifest, artifacts)),
            patch.object(publish.finalizer, "finalize", return_value=accepted),
            patch.object(publish, "expected_transition_state",
                         return_value=(json.loads((packet / "models.after.json").read_text()),
                                       {"policy": "active"},
                                       {"schema_version": "0.1", "enabled": True})),
            patch.object(publish.execute.r11, "STATE", state),
            patch.object(publish.execute.r11, "current_models",
                         return_value=json.loads((packet / "models.after.json").read_text())),
        )
        # A real process death does not execute Python's exception rollback.
        # Reproduce its durable state by first deriving a completed pointer,
        # then restoring only the release pointer to the saved predecessor.
        with common[0], common[1], common[2], common[3], common[4]:
            publish.publish(**args)
        release_after = (owner / "current-release.json").read_bytes()
        runtime_after = (owner / "current-runtime-config.json").read_bytes()
        (owner / "current-release.json").write_bytes(
            (packet / "previous-current-release.json").read_bytes())
        args["receipt_path"].unlink()
        with common[0], common[1], common[2], common[3], common[4]:
            result = publish.publish(**args)
            args["receipt_path"].unlink()
            replay = publish.publish(**args)
        self.assertEqual(result["status"], "published_verified")
        self.assertEqual(replay["status"], "publication_already_complete")
        self.assertTrue(args["receipt_path"].is_file())
        self.assertEqual((owner / "current-release.json").read_bytes(), release_after)
        self.assertEqual((owner / "current-runtime-config.json").read_bytes(), runtime_after)


if __name__ == "__main__":
    unittest.main()
