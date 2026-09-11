from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.prepare_release_acceptance_candidate import (
    ARTIFACT_NAMES,
    CandidateError,
    _canonical_sha256,
    build_candidate,
    template,
    write_exclusive,
)


class ReleaseAcceptanceCandidateTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.packet = self.root / "packet"
        self.source.mkdir()
        self.packet.mkdir()
        subprocess.run(["git", "init", "-q", str(self.source)], check=True)
        (self.source / "tracked.txt").write_text("frozen\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.source), "add", "tracked.txt"], check=True)
        subprocess.run(
            [
                "git", "-C", str(self.source), "-c", "user.name=Dalton Test",
                "-c", "user.email=dalton@example.invalid", "commit", "-qm", "freeze",
            ],
            check=True,
        )
        self.commit = subprocess.check_output(
            ["git", "-C", str(self.source), "rev-parse", "HEAD"], text=True
        ).strip()

    def _write(self, name: str, value: bytes) -> dict[str, str]:
        path = self.packet / name
        path.write_bytes(value)
        return {"path": name, "sha256": hashlib.sha256(value).hexdigest()}

    def _complete(self, count: int = 15) -> dict[str, object]:
        document = template()
        document.update(
            release_ref="R11a",
            status="candidate_inputs_complete",
            packet_root=str(self.packet),
            source={"root": str(self.source), "commit": self.commit},
        )
        snapshot = {
            f"role-{index}-model-config.json": {"routing_policy_ref": f"route:{index}"}
            for index in range(count)
        }
        snapshot_bytes = (json.dumps(snapshot, indent=2) + "\n").encode()
        latest = {
            "schema_version": "0.1",
            "snapshot_id": "20260911T103234.044991Z",
            "created_at": "2026-09-11T10:32:53+00:00",
            "files": [{"name": "core", "file": "core.sqlite", "sha256": "a" * 64, "size_bytes": 1}],
            "status": "fresh",
        }
        retention = {
            "status": "pruned",
            "keep_latest": 3,
            "retained_snapshot_ids": [
                "20260911T103234.044991Z",
                "20260911T100231.684487Z",
                "20260911T071935.194784Z",
            ],
            "deleted_snapshot_ids": [],
        }
        artifacts = {}
        suite_log = b"test output\nRan 370 tests in 1.000s\n\nOK (skipped=1)\n"
        suite_runner = b"# guarded full-suite runner\n"
        native_result = {
            "tests": 370,
            "failures": 0,
            "errors": 0,
            "skipped": 1,
            "expected_failures": 0,
            "unexpected_successes": 0,
            "successful": True,
        }
        native_result_bytes = (json.dumps(native_result) + "\n").encode()
        for name in ARTIFACT_NAMES:
            if name == "final_activated_model_config_snapshot":
                artifacts[name] = self._write("final-model-configs.json", snapshot_bytes)
            elif name == "latest_backup_manifest":
                artifacts[name] = self._write(
                    "latest-backup-manifest.json",
                    (json.dumps(latest, sort_keys=True) + "\n").encode(),
                )
            elif name == "backup_retention_receipt":
                artifacts[name] = self._write(
                    "backup-retention.json",
                    (json.dumps(retention, sort_keys=True) + "\n").encode(),
                )
            elif name == "service_config_before_snapshot":
                service = {
                    "schema_version": "0.1",
                    "plugins": ["existing"],
                    "backup": {"enabled": True, "root": "/state/backups", "interval_seconds": 86400},
                }
                artifacts[name] = self._write(name + ".json", (json.dumps(service) + "\n").encode())
            elif name == "service_config_after_candidate":
                service = {
                    "schema_version": "0.1",
                    "plugins": ["existing"],
                    "backup": {
                        "enabled": True, "root": "/state/backups",
                        "interval_seconds": 86400, "keep_latest": 3,
                    },
                }
                artifacts[name] = self._write(name + ".json", (json.dumps(service) + "\n").encode())
            elif name == "full_suite_log":
                artifacts[name] = self._write("full-suite.log", suite_log)
            elif name == "full_suite_receipt":
                receipt = {
                    "status": "passed",
                    "source_root": str(self.source),
                    "code_commit": self.commit,
                    "final_commit": self.commit,
                    "clean": True,
                    "command": ["/runtime/python", "/tmp/full-suite-runner.py", "--child"],
                    "runner_sha256": hashlib.sha256(suite_runner).hexdigest(),
                    "discovery": {"start_dir": "tests", "top_level_dir": "."},
                    "elapsed_seconds": 1.0,
                    "exit_code": 0,
                    **native_result,
                    "log": "/tmp/original-full-suite.log",
                    "log_sha256": hashlib.sha256(suite_log).hexdigest(),
                    "native_result": "/tmp/full-suite-native-result.json",
                    "native_result_sha256": hashlib.sha256(native_result_bytes).hexdigest(),
                }
                artifacts[name] = self._write(
                    "full-suite-receipt.json", (json.dumps(receipt) + "\n").encode()
                )
            elif name == "full_suite_runner":
                artifacts[name] = self._write("full-suite-runner.py", suite_runner)
            elif name == "full_suite_native_result":
                artifacts[name] = self._write("full-suite-native-result.json", native_result_bytes)
            else:
                artifacts[name] = self._write(f"{name}.artifact", f"{name}\n".encode())
        document["artifacts"] = artifacts
        document["runtime_configuration"] = {
            "model_config_count": count,
            "semantic_snapshot_sha256": _canonical_sha256(snapshot),
        }
        document["latest_backup"] = {"snapshot_id": latest["snapshot_id"]}
        return document

    def test_template_is_inert_and_has_no_frozen_guess(self) -> None:
        document = template()
        self.assertEqual(document["acceptance_state"], "pending")
        self.assertEqual(document["deployment_state"], "not_started")
        self.assertFalse(any(row["sha256"] for row in document["artifacts"].values()))
        self.assertIsNone(document["runtime_configuration"]["model_config_count"])
        with self.assertRaisesRegex(CandidateError, "release ref is unresolved"):
            build_candidate(document)

    def test_complete_final_snapshot_builds_only_an_unaccepted_candidate(self) -> None:
        candidate = build_candidate(self._complete(count=15))
        self.assertEqual(candidate["status"], "candidate_pending_owner_acceptance")
        self.assertEqual(candidate["acceptance_state"], "pending")
        self.assertEqual(candidate["deployment_state"], "not_started")
        self.assertEqual(candidate["packet_root"], str(self.packet.resolve()))
        self.assertEqual(candidate["runtime_configuration"]["model_config_count"], 15)
        self.assertEqual(candidate["full_suite"]["tests"], 370)
        self.assertEqual(candidate["latest_backup"]["snapshot_id"], "20260911T103234.044991Z")
        self.assertEqual(
            candidate["deployment_operations"]["backup_retention"]["phase"],
            "after_runtime_install_before_controller_start",
        )
        unsigned = dict(candidate)
        recorded = unsigned.pop("content_hash")
        self.assertEqual(recorded, _canonical_sha256(unsigned))
        self.assertNotIn("accepted_manifest_sha256", candidate)

    def test_runtime_count_is_derived_from_final_snapshot_not_a_stage_constant(self) -> None:
        document = self._complete(count=14)
        document["runtime_configuration"]["model_config_count"] = 15
        with self.assertRaisesRegex(CandidateError, "count differs"):
            build_candidate(document)

    def test_hashed_failure_receipt_cannot_be_called_a_full_suite_pass(self) -> None:
        document = self._complete()
        row = document["artifacts"]["full_suite_receipt"]
        path = self.packet / row["path"]
        receipt = json.loads(path.read_text())
        receipt["status"] = "failed"
        receipt["exit_code"] = 1
        path.write_text(json.dumps(receipt), encoding="utf-8")
        row["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(CandidateError, "did not pass cleanly"):
            build_candidate(document)

    def test_suite_count_must_match_the_bound_log(self) -> None:
        document = self._complete()
        row = document["artifacts"]["full_suite_receipt"]
        path = self.packet / row["path"]
        receipt = json.loads(path.read_text())
        receipt["tests"] = 371
        path.write_text(json.dumps(receipt), encoding="utf-8")
        row["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(CandidateError, "counts differ"):
            build_candidate(document)

    def test_service_config_candidate_cannot_hide_an_unrelated_change(self) -> None:
        document = self._complete()
        row = document["artifacts"]["service_config_after_candidate"]
        path = self.packet / row["path"]
        value = json.loads(path.read_text())
        value["plugins"] = ["changed"]
        path.write_text(json.dumps(value), encoding="utf-8")
        row["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(CandidateError, "changes more than"):
            build_candidate(document)

    def test_old_archive_inventory_cannot_be_bound_as_retention_evidence(self) -> None:
        document = self._complete()
        row = document["artifacts"]["backup_retention_receipt"]
        path = self.packet / row["path"]
        retention = json.loads(path.read_text())
        retention["old_archive"] = "deleted-r9a.tar.gz"
        path.write_text(json.dumps(retention), encoding="utf-8")
        row["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(CandidateError, "references an archive"):
            build_candidate(document)

    def test_snapshot_hash_drift_stops_before_candidate_creation(self) -> None:
        document = self._complete()
        row = document["artifacts"]["openclaw_config_snapshot"]
        (self.packet / row["path"]).write_text("changed\n", encoding="utf-8")
        with self.assertRaisesRegex(CandidateError, "SHA-256 changed"):
            build_candidate(document)

    def test_dirty_source_is_not_a_freeze(self) -> None:
        document = self._complete()
        (self.source / "untracked.txt").write_text("dirty\n", encoding="utf-8")
        with self.assertRaisesRegex(CandidateError, "not clean"):
            build_candidate(document)

    def test_exclusive_write_preserves_an_existing_owner_candidate(self) -> None:
        output = self.root / "r11.candidate.json"
        output.write_text("owner\n", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            write_exclusive(output, {"status": "candidate"})
        self.assertEqual(output.read_text(encoding="utf-8"), "owner\n")

    def test_closed_artifact_inventory_rejects_legacy_broker_bytes(self) -> None:
        document = self._complete()
        document = copy.deepcopy(document)
        document["artifacts"]["r9a_broker_plugin_tree"] = {
            "path": "old",
            "sha256": "0" * 64,
        }
        with self.assertRaisesRegex(CandidateError, "artifact inventory differs"):
            build_candidate(document)


if __name__ == "__main__":
    unittest.main()
