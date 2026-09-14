from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dalton_core.store import content_hash
from dalton_core.workspace_creation import (
    SCHEMA_VERSION, connection_catalog_projection, create_blank_workspace,
)
from dalton_core.workspace_runtime import ENVIRONMENT_KEY


class BlankWorkspaceCreationTests(unittest.TestCase):
    def setUp(self) -> None:
        platform = patch("dalton_core.workspace_creation.sys.platform", "test")
        platform.start()
        self.addCleanup(platform.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.host = Path(self.temp.name) / "Dalton"
        self.release = self.host / "runtime" / "releases" / ("a" * 64)
        self.release.mkdir(parents=True)

    def test_fresh_authorities_seed_only_connection_definitions(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ENVIRONMENT_KEY, None)
            result = create_blank_workspace(
                self.host, "blank", 18881, "release:sha256:" + "a" * 64, self.release,
                request_id="workspace-request:one", display_name="Blank Research",
            )
        self.assertEqual(result["state"], "awaiting_mission")
        self.assertFalse(result["research_state_copied"])
        root = self.host / "workspaces" / "blank"
        self.assertTrue((root / "state/dalton-core/writer-tokens.json").is_file())
        with sqlite3.connect(root / "state/dalton-core/model-router.sqlite") as db:
            self.assertEqual(db.execute("SELECT count(*) FROM model_endpoint_profile_versions").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM model_route_decisions").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM model_routing_policy_versions").fetchone()[0], 0)
        with sqlite3.connect(root / "state/dalton-core/core.sqlite") as db:
            self.assertEqual(db.execute("SELECT count(*) FROM connector_invocations").fetchone()[0], 0)
            approval_like = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND "
                "(name LIKE '%approval%' OR name LIKE '%review_decision%')"
            ).fetchall()
            for (table,) in approval_like:
                self.assertEqual(db.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0], 0)
        metadata = json.loads((root / "display.json").read_text())
        self.assertEqual(metadata["display_name"], "Blank Research")

    def test_cli_rejects_inherited_workspace_binding(self):
        request = {
            "schema_version": SCHEMA_VERSION, "request_id": "workspace-request:cli",
            "display_name": "CLI Blank", "host_root": str(self.host), "slug": "cli-blank",
            "cockpit_port": 18882, "release_ref": "release:sha256:" + "a" * 64,
            "release_path": str(self.release), "shared_readonly_paths": [],
            "shared_model_capacity_bindings": [], "shared_connector_capacity": [],
            "connection_catalog": None,
        }
        path = Path(self.temp.name) / "request.json"
        path.write_text(json.dumps(request))
        completed = subprocess.run(
            [os.environ.get("DALTON_TEST_PYTHON", os.sys.executable), "-m",
             "dalton_core.workspace_creation", "--request", str(path)],
            capture_output=True, text=True, check=False,
            env={**os.environ, ENVIRONMENT_KEY: "/wrong/workspace.json",
                 "PYTHONPATH": str(Path(__file__).parents[1] / "src")},
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertFalse((self.host / "workspaces/cli-blank").exists())

    def test_shared_catalog_is_pinned_readonly_without_local_authority(self):
        catalog_dir = self.host / "connections"
        catalog_dir.mkdir(parents=True)
        body = {
            "schema_version": "dalton-shared-connection-catalog-0.1",
            "models": [{
                "id": "connection:model:shared", "provider": "openai", "model": "gpt-test",
                "family": "gpt-test", "adapter_ref": "adapter:openclaw-simple-completion:0.1",
                "credential_slot_ref": "credential-slot:openai:shared",
                "capabilities": ["research"], "modalities": ["text"],
                "transport": {"kind": "broker", "endpoint_ref": "broker:shared",
                              "socket_path": None, "config_path": None},
            }],
            "sources": [{
                "id": "connection:source:sec", "connector_ref": "connector:sec",
                "capability_id": "capability:dalton:connector:sec-edgar", "auth_mode": "none",
                "credential_slot_refs": [], "allowed_operations": ["company-facts"],
                "allowed_hosts": ["data.sec.gov"],
                "transport": {"kind": "https", "endpoint_ref": "endpoint:sec",
                              "socket_path": None, "config_path": None},
            }],
        }
        catalog = {**body, "content_hash": content_hash(body)}
        path = catalog_dir / "catalog.json"
        path.write_text(json.dumps(catalog))
        result = create_blank_workspace(
            self.host, "catalog", 18884, "release:sha256:" + "a" * 64, self.release,
            request_id="workspace-request:catalog", display_name="Catalog",
            shared_readonly_paths=[catalog_dir],
            connection_catalog={"path": str(path), "content_hash": catalog["content_hash"]},
        )
        self.assertEqual(result["connection_catalog"]["model_count"], 1)
        root = self.host / "workspaces/catalog/state/dalton-core"
        with sqlite3.connect(root / "model-router.sqlite") as db:
            self.assertEqual(db.execute("SELECT count(*) FROM model_endpoint_profile_versions").fetchone()[0], 0)
        with sqlite3.connect(root / "core.sqlite") as db:
            self.assertEqual(db.execute("SELECT count(*) FROM connector_profile_versions").fetchone()[0], 0)
        projection = connection_catalog_projection(
            self.host / "workspaces/catalog/workspace.json")
        self.assertTrue(projection["available"])
        self.assertEqual(projection["models"][0]["id"], "connection:model:shared")
        repeated = create_blank_workspace(
            self.host, "catalog", 18884, "release:sha256:" + "a" * 64, self.release,
            request_id="workspace-request:catalog", display_name="Catalog",
            shared_readonly_paths=[catalog_dir],
            connection_catalog={"path": str(path), "content_hash": catalog["content_hash"]},
        )
        self.assertEqual(repeated["workspace_id"], result["workspace_id"])

    def test_request_refuses_authority_seed_state(self):
        with self.assertRaisesRegex(Exception, "invalid closed shape"):
            from dalton_core.workspace_creation import create_from_request
            create_from_request({"schema_version": SCHEMA_VERSION, "approvals": []})

    def test_darwin_writer_socket_limit_fails_before_workspace_write(self):
        long_host = Path(self.temp.name) / ("long-host-name-" * 7)
        release = long_host / "runtime/releases" / ("a" * 64)
        release.mkdir(parents=True)
        with patch("dalton_core.workspace_creation.sys.platform", "darwin"):
            with self.assertRaisesRegex(Exception, "writer socket"):
                create_blank_workspace(
                    long_host, "blank", 18885, "release:sha256:" + "a" * 64, release,
                    request_id="workspace-request:long", display_name="Long",
                )
        self.assertFalse((long_host / "workspaces/blank").exists())
