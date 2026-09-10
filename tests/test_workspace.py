from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

from dalton_core.bootstrap import bootstrap
from dalton_core.controller_singleton import resident_controllers
from dalton_core.service import ServiceConfig, ServiceConfigError
from dalton_core.workspace import (
    WorkspaceError,
    WorkspacePaths,
    create_workspace_manifest,
    dry_run_legacy_migration,
    load_workspace_manifest,
    validate_service_mapping_paths,
    workspace_manifest,
)


RELEASE_HASH = "a" * 64
RELEASE_REF = "release:sha256:" + RELEASE_HASH


def _create_workspace_process(host, release, slug, port, start, queue):
    start.wait()
    try:
        result = create_workspace_manifest(host, slug, port, RELEASE_REF, release)
        queue.put(("ok", result.slug))
    except Exception as exc:
        queue.put((type(exc).__name__, str(exc)))


class WorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.host = Path(self.temp.name) / "Dalton"
        self.release = self.host / "runtime" / "releases" / RELEASE_HASH
        self.release.mkdir(parents=True)

    def create(self, slug: str, port: int, **kwargs) -> WorkspacePaths:
        return create_workspace_manifest(
            self.host, slug, port, RELEASE_REF, self.release, **kwargs)

    def test_two_real_bootstraps_are_disjoint_and_idempotent(self):
        one = self.create("analyst-a", 8787)
        two = self.create("analyst-b", 8788)
        first = bootstrap(one.state_dir, one.config_path,
                          workspace_manifest=one.manifest_path)
        token_hash = hashlib.sha256(Path(first["token_config"]).read_bytes()).hexdigest()
        bootstrap(one.state_dir, one.config_path,
                  workspace_manifest=one.manifest_path)
        bootstrap(two.state_dir, two.config_path,
                  workspace_manifest=two.manifest_path)
        self.assertEqual(token_hash,
                         hashlib.sha256(Path(first["token_config"]).read_bytes()).hexdigest())
        self.assertNotEqual(one.workspace_id, two.workspace_id)
        self.assertNotEqual(one.writer_socket, two.writer_socket)
        self.assertNotEqual(one.config_path, two.config_path)
        self.assertTrue(one.state_dir.joinpath("core.sqlite").is_file())
        self.assertTrue(two.state_dir.joinpath("core.sqlite").is_file())
        config = ServiceConfig.from_file(one.config_path)
        self.assertEqual(config.workspace.workspace_id, one.workspace_id)
        connection = sqlite3.connect(
            f"file:{one.state_dir / 'core.sqlite'}?mode=ro", uri=True)
        try:
            connection.execute("PRAGMA query_only=ON")
            self.assertGreater(connection.execute(
                "SELECT count(*) FROM sqlite_master").fetchone()[0], 0)
        finally:
            connection.close()

    def test_duplicate_identity_port_slug_and_symlink_escape_refuse_prewrite(self):
        identity = str(uuid.uuid4())
        self.create("analyst-a", 8787, workspace_id=identity)
        cases = (
            ("analyst-b", 8788, identity, "workspace_id"),
            ("analyst-b", 8787, str(uuid.uuid4()), "cockpit_port"),
            ("analyst-a", 8789, str(uuid.uuid4()), "already exists"),
        )
        for slug, port, candidate_id, reason in cases:
            with self.subTest(reason=reason), self.assertRaisesRegex(WorkspaceError, reason):
                self.create(slug, port, workspace_id=candidate_id)
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        escape = self.host / "workspaces" / "escape"
        escape.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(WorkspaceError, "outside the host|dangerously broad"):
            self.create("escape", 8790)
        self.assertFalse((outside / "workspace.json").exists())

    def test_manifest_tamper_alias_and_undeclared_service_path_fail_closed(self):
        workspace = self.create("analyst-a", 8787)
        raw = json.loads(workspace.manifest_path.read_text())
        raw["state_dir"] = str(workspace.workspace_root / "other")
        with self.assertRaisesRegex(WorkspaceError, "content hash"):
            WorkspacePaths.from_manifest(raw)
        with self.assertRaisesRegex(WorkspaceError, "boundary"):
            validate_service_mapping_paths(
                {"broker_socket": str(Path(self.temp.name) / "other.sock")}, workspace)
        validate_service_mapping_paths(
            {"broker_socket": str(self.release / "broker.sock")}, workspace)
        with self.assertRaisesRegex(WorkspaceError, "boundary"):
            validate_service_mapping_paths(
                {"candidate_staging_path": str(self.release / "must-not-write.sqlite")},
                workspace)

    def test_shared_readonly_roots_cannot_cover_host_or_any_workspace(self):
        for unsafe in (Path("/"), self.host, self.host / "workspaces",
                       self.host / "workspaces" / "other"):
            with self.subTest(path=unsafe), self.assertRaisesRegex(
                    WorkspaceError, "dangerously broad"):
                workspace_manifest(
                    self.host, "analyst-a", 8787, RELEASE_REF, self.release,
                    shared_readonly_paths=[unsafe])

    def test_shared_capacity_is_explicit_host_binding_or_absent_unknown(self):
        plain = self.create("analyst-a", 8787)
        self.assertIsNone(plain.shared_capacity)
        database = self.host / "fleet-capacity" / "vendor.sqlite"
        governed = self.create(
            "analyst-b", 8788,
            shared_capacity={"database": str(database),
                             "policy_ref": "vendor-capacity-policy:one",
                             "policy_hash": "b" * 64},
        )
        self.assertEqual(governed.shared_capacity["database"], str(database))
        with self.assertRaisesRegex(WorkspaceError, "fleet-capacity"):
            self.create(
                "analyst-c", 8789,
                shared_capacity={"database": str(self.host / "other.sqlite"),
                                 "policy_ref": "vendor-capacity-policy:one",
                                 "policy_hash": "b" * 64},
            )

    def test_create_cli_accepts_repeated_connector_capacity_bindings(self):
        bindings = []
        for index in range(2):
            path = Path(self.temp.name) / f"connector-{index}.json"
            path.write_text(json.dumps({
                "database": str(self.host / "fleet-capacity" / f"connector-{index}.sqlite"),
                "policy_ref": f"connector-capacity-policy:{index}",
                "policy_hash": chr(ord("b") + index) * 64,
            }))
            bindings.append(path)
        command = [
            sys.executable, "-m", "dalton_core.workspace", "create",
            "--host-root", str(self.host), "--slug", "analyst-cli",
            "--cockpit-port", "8791", "--release-ref", RELEASE_REF,
            "--release-path", str(self.release),
        ]
        for path in bindings:
            command.extend(["--shared-connector-capacity-binding", str(path)])
        completed = subprocess.run(
            command, capture_output=True, text=True, check=False,
            env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")},
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        workspace = load_workspace_manifest(result["manifest"])
        self.assertEqual(len(workspace.shared_connector_capacity), 2)
        self.assertEqual(
            [row["policy_ref"] for row in workspace.shared_connector_capacity],
            ["connector-capacity-policy:0", "connector-capacity-policy:1"],
        )

    def test_create_cli_rejects_bad_connector_bindings_before_workspace_write(self):
        valid = {
            "database": str(self.host / "fleet-capacity" / "connector.sqlite"),
            "policy_ref": "connector-capacity-policy:one",
            "policy_hash": "b" * 64,
        }
        cases = {
            "malformed": "{",
            "unsupported": json.dumps({**valid, "daily_limit": 100}),
            "duplicate": json.dumps(valid),
        }
        for name, body in cases.items():
            with self.subTest(name=name):
                case_host = Path(self.temp.name) / name / "Dalton"
                existing = create_workspace_manifest(
                    case_host, "existing", 8892, RELEASE_REF, self.release)
                existing_bytes = existing.manifest_path.read_bytes()
                binding = Path(self.temp.name) / f"{name}.json"
                binding.write_text(body)
                command = [
                    sys.executable, "-m", "dalton_core.workspace", "create",
                    "--host-root", str(case_host), "--slug", "must-not-exist",
                    "--cockpit-port", "8792", "--release-ref", RELEASE_REF,
                    "--release-path", str(self.release),
                    "--shared-connector-capacity-binding", str(binding),
                ]
                if name == "duplicate":
                    command.extend(["--shared-connector-capacity-binding", str(binding)])
                completed = subprocess.run(
                    command, capture_output=True, text=True, check=False,
                    env={**os.environ,
                         "PYTHONPATH": str(Path(__file__).parents[1] / "src")},
                )
                self.assertNotEqual(completed.returncode, 0)
                self.assertFalse(
                    (case_host / "workspaces" / "must-not-exist").exists()
                )
                self.assertEqual(existing.manifest_path.read_bytes(), existing_bytes)

    def test_service_binding_rejects_manifest_or_path_substitution(self):
        workspace = self.create("analyst-a", 8787)
        bootstrap(workspace.state_dir, workspace.config_path,
                  workspace_manifest=workspace.manifest_path)
        raw = json.loads(workspace.config_path.read_text())
        raw["core_db"] = str(workspace.state_dir / "other.sqlite")
        workspace.config_path.write_text(json.dumps(raw))
        with self.assertRaisesRegex(ServiceConfigError, "bound workspace"):
            ServiceConfig.from_file(workspace.config_path)

    def test_bootstrap_path_mismatch_refuses_before_creating_state(self):
        workspace = self.create("analyst-a", 8787)
        wrong = workspace.workspace_root / "wrong-state"
        with self.assertRaisesRegex(RuntimeError, "do not match"):
            bootstrap(wrong, workspace.config_path,
                      workspace_manifest=workspace.manifest_path)
        self.assertFalse(wrong.exists())

    def test_dry_run_migration_is_stable_and_writes_nothing(self):
        legacy_state = self.host / "state" / "dalton-core"
        legacy_config = self.host / "config" / "service.json"
        first = dry_run_legacy_migration(
            self.host, legacy_state, legacy_config, "default", 8787,
            RELEASE_REF, self.release)
        second = dry_run_legacy_migration(
            self.host, legacy_state, legacy_config, "default", 8787,
            RELEASE_REF, self.release)
        self.assertEqual(first, second)
        self.assertFalse(first["writes_performed"])
        self.assertFalse((self.host / "workspaces" / "default").exists())

    def test_controller_scan_distinguishes_workspace_configs(self):
        one = self.create("analyst-a", 8787)
        two = self.create("analyst-b", 8788)

        class Result:
            returncode = 0
            stderr = ""
            stdout = (f"101 python3 python3 -m dalton_core.service --config {one.config_path}\n"
                      f"102 python3 python3 -m dalton_core.service --config {two.config_path}\n")

        self.assertEqual(resident_controllers(one.config_path, self_pid=999,
                                              run=lambda *a, **k: Result()), [101])
        self.assertEqual(resident_controllers(two.config_path, self_pid=999,
                                              run=lambda *a, **k: Result()), [102])

    def test_registry_lock_serializes_cross_slug_port_collision(self):
        context = multiprocessing.get_context("spawn")
        start, queue = context.Event(), context.Queue()
        processes = [context.Process(
            target=_create_workspace_process,
            args=(str(self.host), str(self.release), f"analyst-{letter}", 8787,
                  start, queue),
        ) for letter in ("a", "b")]
        for process in processes:
            process.start()
        start.set()
        results = [queue.get(timeout=15) for _ in processes]
        for process in processes:
            process.join(15)
            self.assertEqual(process.exitcode, 0)
        self.assertEqual([row[0] for row in results].count("ok"), 1)
        self.assertEqual([row[0] for row in results].count("WorkspaceError"), 1)
        self.assertIn("cockpit_port", next(row[1] for row in results
                                           if row[0] == "WorkspaceError"))


if __name__ == "__main__":
    unittest.main()
