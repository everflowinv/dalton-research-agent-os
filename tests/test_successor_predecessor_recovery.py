from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts.successor_predecessor_recovery import (
    ARTIFACT_KEYS, PredecessorRecoveryError, build_recovery_proof,
    validate_recovery_proof, verify_installed_predecessor,
)


def write(path: Path, value) -> None:
    if isinstance(value, bytes): path.write_bytes(value)
    else: path.write_text(json.dumps(value, sort_keys=True) + "\n")


def build(paths):
    return build_recovery_proof(**{f"{name}_path": path for name, path in paths.items()})


class PredecessorRecoveryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=Path.cwd()); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name); self.paths = {name: self.root / name for name in ARTIFACT_KEYS}
        commit = "b" * 40; wheel = b"wheel"
        write(self.paths["accepted_wheel"], wheel)
        previous = {"release_ref": "R19", "source_commit": "9" * 40}
        write(self.paths["published_previous_pointer"], previous)
        previous_sha = hashlib.sha256(self.paths["published_previous_pointer"].read_bytes()).hexdigest()
        pointer = {"schema_version": "dalton-current-release-0.2", "status": "deployed_verified",
                   "release_ref": "R20", "source_commit": "a" * 40,
                   "current_runtime_config_sha256": "c" * 64,
                   "previous_release": {**previous, "pointer_sha256": previous_sha}}
        write(self.paths["published_pointer"], pointer)
        manifest = {"status": "accepted_for_stopped_window", "release_ref": "R21",
                    "source": {"commit": commit}, "artifacts": {"wheel": {
                        "sha256": hashlib.sha256(wheel).hexdigest()}}}
        write(self.paths["failed_manifest"], manifest)
        manifest_sha = hashlib.sha256(self.paths["failed_manifest"].read_bytes()).hexdigest()
        deployment = {"schema_version": "successor-stopped-window-execution-0.1",
                      "status": "deployment_failed", "source_commit": commit,
                      "candidate_manifest_sha256": manifest_sha,
                      "rollback": {"status": "rollback_failed"}}
        write(self.paths["failed_deployment"], deployment)
        deployment_sha = hashlib.sha256(self.paths["failed_deployment"].read_bytes()).hexdigest()
        external = {"status": "verified_read_only_inert", "candidate_commit": commit,
                    "live_mutation": False, "external_mutations_authorized": 0,
                    "model_calls": 0, "network_calls": 0,
                    "published_owner_predecessor_release": "R20",
                    "external_origin_release": "foundation-r18b",
                    "external_origin_acceptance": "runtime_installed_health_failed_unpublished",
                    "r18b_source_commit": "8" * 40, "r18b_transition_sha256": "1" * 64,
                    "r18b_outer_receipt_sha256": "9" * 64,
                    "r18b_inner_receipt_sha256": "a" * 64,
                    "r18b_installed_sha256": "b" * 64,
                    "openclaw_config_sha256": "2" * 64, "host_target_sha256": "3" * 64,
                    "broker_tree_sha256": "4" * 64, "host_helper_sha256": "5" * 64}
        external["content_hash"] = hashlib.sha256((json.dumps(
            external, sort_keys=True, separators=(",", ":")) + "\n").encode()).hexdigest()
        write(self.paths["external_before"], external); write(self.paths["external_after"], external)
        external_sha = hashlib.sha256(self.paths["external_after"].read_bytes()).hexdigest()
        prestart = {"schema_version": "foundation-r21-recovery-prestart-verification-0.1",
                    "status": "verified_failed_deployment_runtime_safe_to_restart",
                    "source_commit": commit, "failed_deployment_sha256": deployment_sha,
                    "accepted_manifest_sha256": manifest_sha,
                    "accepted_wheel_sha256": hashlib.sha256(wheel).hexdigest(),
                    "runtime_matches_accepted_wheel": True, "runtime_file_count": 7,
                    "model_configs_exact": True, "model_config_count": 2,
                    "service_config_exact": True, "openclaw_config_exact": True,
                    "protected_state_excluding_writer_tokens_exact": True,
                    "protected_entries_excluding_writer_tokens": 3,
                    "reviewed_plist_sha256": {"writer": "6" * 64},
                    "writer_tokens": {"snapshot_sha256": "7" * 64, "live_sha256": "8" * 64,
                        "principal_inventory_exact": True, "non_core_principals_exact": True,
                        "core_token_equal": True, "core_non_operation_fields_exact": True,
                        "before_operation_count": 2, "after_operation_count": 3,
                        "only_added_operation": "new", "after_equals_source_core_operations": True},
                    "external_dependency_receipt_sha256": external_sha,
                    "boundaries": {"database_restore_performed": False,
                        "metadata_overwrite_performed": False, "service_calls": False,
                        "provider_calls": False, "live_mutation": False}}
        prestart["content_hash"] = hashlib.sha256((json.dumps(
            prestart, sort_keys=True, separators=(",", ":")) + "\n").encode()).hexdigest()
        write(self.paths["recovery_prestart"], prestart)
        prestart_sha = hashlib.sha256(self.paths["recovery_prestart"].read_bytes()).hexdigest()
        restart = {"schema_version": "foundation-r21-service-recovery-0.1",
                   "status": "verified_installed_runtime_restarted_healthy",
                   "source_commit": commit, "failed_deployment_sha256": deployment_sha,
                   "prestart_proof_sha256": prestart_sha, "published_owner_release": "R20",
                   "deployment_reclassified": False, "database_restore_performed": False,
                   "metadata_overwrite_performed": False}
        write(self.paths["recovery_restart"], restart)
        write(self.paths["model_snapshot"], {"a.json": {"model": "x"}})
        write(self.paths["service_snapshot"], b'{"service":true}\n')
        write(self.paths["openclaw_snapshot"], b'{"openclaw":true}\n')
        self.paths["rollback_state"].mkdir()
        write(self.paths["rollback_state"] / "owner.json", {"owner": "state"})
        published = {
            "published_manifest": {"status": "accepted_for_stopped_window"},
            "published_deployment": {"status": "installer_finished_runtime_health_pending"},
            "published_installed": {"status": "installed_bytes_verified_runtime_pending"},
            "published_health": {"status": "passed"},
            "published_finalization": {"status": "passed_pending_publication"},
        }
        for name, value in published.items(): write(self.paths[name], value)
        pointer.update({
            "candidate_manifest_sha256": hashlib.sha256(
                self.paths["published_manifest"].read_bytes()).hexdigest(),
            "deployment_receipt_sha256": hashlib.sha256(
                self.paths["published_deployment"].read_bytes()).hexdigest(),
            "installed_verification_sha256": hashlib.sha256(
                self.paths["published_installed"].read_bytes()).hexdigest(),
            "health_summary_sha256": hashlib.sha256(
                self.paths["published_health"].read_bytes()).hexdigest(),
            "finalization_sha256": hashlib.sha256(
                self.paths["published_finalization"].read_bytes()).hexdigest(),
        })
        write(self.paths["published_pointer"], pointer)
        write(self.paths["published_publication"], {
            "status": "published_verified", "current_release_sha256": hashlib.sha256(
                self.paths["published_pointer"].read_bytes()).hexdigest()})

    def test_build_and_validate_closed_redacted_split_identity(self):
        proof = build(self.paths)
        self.assertEqual("deployment_failed", proof["failed_install"]["deployment_status"])
        self.assertEqual("rollback_failed", proof["failed_install"]["rollback_status"])
        self.assertNotIn("secret", json.dumps(proof).lower())
        self.assertEqual(proof, validate_recovery_proof(proof, artifacts=self.paths))
        installed = proof["recovery"]["installed_identity"]
        self.assertEqual(installed, verify_installed_predecessor(proof, installed))

    def test_forged_or_transplanted_chain_receipts_fail(self):
        for name, field, value in (
            ("failed_deployment", "source_commit", "c" * 40),
            ("recovery_prestart", "failed_deployment_sha256", "0" * 64),
            ("recovery_restart", "published_owner_release", "R19"),
            ("external_after", "r18b_source_commit", "7" * 40),
        ):
            with self.subTest(name=name):
                original = self.paths[name].read_bytes(); row = json.loads(original)
                row[field] = value; write(self.paths[name], row)
                with self.assertRaises(PredecessorRecoveryError): build(self.paths)
                self.paths[name].write_bytes(original)

    def test_changed_bytes_missing_links_and_symlinks_fail(self):
        proof = build(self.paths)
        forged = copy.deepcopy(proof); forged["failed_install"]["deployment_status"] = "succeeded"
        with self.assertRaisesRegex(PredecessorRecoveryError, "proof differs"):
            validate_recovery_proof(forged, artifacts=self.paths)
        self.paths["accepted_wheel"].write_bytes(b"changed")
        with self.assertRaisesRegex(PredecessorRecoveryError, "wheel"):
            validate_recovery_proof(proof, artifacts=self.paths)
        self.paths["accepted_wheel"].write_bytes(b"wheel")
        target = self.paths["external_before"]; actual = self.root / "external-real"
        actual.write_bytes(target.read_bytes()); target.unlink(); target.symlink_to(actual)
        with self.assertRaisesRegex(PredecessorRecoveryError, "unsafe"):
            build(self.paths)

    def test_fresh_installed_predecessor_is_separate_and_exact(self):
        proof = build(self.paths)
        changed = copy.deepcopy(proof["recovery"]["installed_identity"])
        changed["runtime_file_count"] += 1
        with self.assertRaisesRegex(PredecessorRecoveryError, "no longer"):
            verify_installed_predecessor(proof, changed)

    def test_boolean_count_and_protected_snapshot_drift_fail(self):
        original = self.paths["recovery_prestart"].read_bytes()
        row = json.loads(original); row["runtime_file_count"] = True
        row["content_hash"] = hashlib.sha256((json.dumps(
            {key: value for key, value in row.items() if key != "content_hash"},
            sort_keys=True, separators=(",", ":")) + "\n").encode()).hexdigest()
        write(self.paths["recovery_prestart"], row)
        restart_original = self.paths["recovery_restart"].read_bytes()
        restart = json.loads(restart_original)
        restart["prestart_proof_sha256"] = hashlib.sha256(
            self.paths["recovery_prestart"].read_bytes()).hexdigest()
        write(self.paths["recovery_restart"], restart)
        with self.assertRaisesRegex(PredecessorRecoveryError, "incomplete"):
            build(self.paths)
        self.paths["recovery_prestart"].write_bytes(original)
        self.paths["recovery_restart"].write_bytes(restart_original)
        proof = build(self.paths)
        write(self.paths["rollback_state"] / "owner.json", {"owner": "changed"})
        with self.assertRaisesRegex(PredecessorRecoveryError, "proof differs"):
            validate_recovery_proof(proof, artifacts=self.paths)

    def test_inventory_is_closed(self):
        proof = build(self.paths)
        paths = dict(self.paths); paths["extra"] = self.root / "extra"
        with self.assertRaisesRegex(PredecessorRecoveryError, "inventory"):
            validate_recovery_proof(proof, artifacts=paths)


if __name__ == "__main__": unittest.main()
