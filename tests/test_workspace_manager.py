from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
import zipfile
import hashlib
from pathlib import Path
from unittest.mock import patch

from dalton_core.workspace import WorkspaceError
from dalton_core.workspace_manager import (
    _config, _serve, create_managed_workspace, list_workspaces, request_create,
)
from dalton_core.cockpit_plane import CockpitConfig


class WorkspaceManagerTests(unittest.TestCase):
    def setUp(self):
        platform = patch("dalton_core.workspace_creation.sys.platform", "test")
        platform.start()
        self.addCleanup(platform.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / 'manager.json'
        self.config = dict(host_root=str(self.root / 'fleet'), release_path=str(self.root / 'release'),
                           release_ref='release:sha256:' + 'a' * 64, owner_login='owner@example.com',
                           tailscale_host='dalton.example.ts.net', tailscale_executable='/usr/bin/tailscale',
                           launch_agents_dir=str(self.root / 'agents'), ports=[18991, 18992])
        self.save()

    def save(self):
        self.path.write_text(json.dumps(self.config))
        self.path.chmod(0o600)

    def test_unconfigured_list_is_explicitly_disabled(self):
        self.assertFalse(list_workspaces(None, 'owner@example.com', None)['can_create'])

    def test_only_configured_owner_can_read_or_create(self):
        with self.assertRaises(PermissionError):
            list_workspaces(self.path, 'other@example.com', None)
        with patch('dalton_core.workspace_manager.subprocess.run') as run:
            with self.assertRaises(PermissionError):
                request_create(self.path, 'other@example.com', {'name':'研究', 'request_id':'abcdefgh'})
            run.assert_not_called()

    def test_requests_cannot_choose_paths_ports_or_copy_research(self):
        for extra in ({'source_workspace':'old'}, {'port':18993}, {'host_root':'/tmp'}, {'mode':'copy'}):
            with self.subTest(extra=extra), self.assertRaises(WorkspaceError):
                request_create(self.path, 'owner@example.com', {'name':'研究', 'request_id':'abcdefgh', **extra})

    def test_subprocess_drops_caller_namespace_without_mutating_parent(self):
        with patch.dict(os.environ, {'DALTON_WORKSPACE_MANIFEST':'/other/workspace.json'}):
            with patch('dalton_core.workspace_manager.subprocess.run', return_value=subprocess.CompletedProcess(
                    [], 0, '{"status":"running"}\n', '')) as run:
                self.assertEqual(request_create(self.path, 'owner@example.com',
                    {'name':' 新研究 ', 'request_id':'abcdefgh'})['status'], 'running')
            self.assertEqual(os.environ['DALTON_WORKSPACE_MANIFEST'], '/other/workspace.json')
        self.assertNotIn('DALTON_WORKSPACE_MANIFEST', run.call_args.kwargs['env'])
        self.assertIn('新研究', run.call_args.args[0])
        self.assertNotIn('shell', run.call_args.kwargs)

    def test_subprocess_errors_do_not_disclose_local_diagnostics(self):
        with patch('dalton_core.workspace_manager.subprocess.run', return_value=subprocess.CompletedProcess(
                [], 1, '', 'SECRET=/private/token')):
            with self.assertRaises(WorkspaceError) as error:
                request_create(self.path, 'owner@example.com', {'name':'研究', 'request_id':'abcdefgh'})
        self.assertNotIn('SECRET', str(error.exception))

    def test_list_filters_owner_and_only_projects_public_fields(self):
        directory = self.root / 'fleet' / 'creation-requests'
        directory.mkdir(parents=True)
        for i, owner in enumerate(('owner@example.com', 'other@example.com')):
            (directory / f'{i}.json').write_text(json.dumps(dict(owner_login=owner, workspace_id=str(i),
                name='研究', status='failed', retryable=True,
                failure_reason='研究环境尚未准备完成，可以安全重试。',
                url=None, token='secret')))
        result = list_workspaces(self.path, 'owner@example.com', '0')
        self.assertEqual(len(result['items']), 1)
        self.assertTrue(result['items'][0]['current'])
        self.assertTrue(result['items'][0]['retryable'])
        self.assertIn('安全重试', result['items'][0]['failure_reason'])
        self.assertNotIn('secret', str(result))

    def test_list_reports_verified_catalog_counts_not_path_truthiness(self):
        from dalton_core.store import content_hash
        catalog_body = {
            'schema_version': 'dalton-shared-connection-catalog-0.1',
            'models': [], 'sources': [],
        }
        catalog = {**catalog_body, 'content_hash': content_hash(catalog_body)}
        catalog_path = self.root / 'connections.json'
        catalog_path.write_text(json.dumps(catalog))
        self.config['connections_path'] = str(catalog_path)
        self.save()
        shared = list_workspaces(
            self.path, 'owner@example.com', None)['shared_connections']
        self.assertEqual(shared, {'models': 0, 'sources': 0, 'available': True})
        catalog['content_hash'] = '0' * 64
        catalog_path.write_text(json.dumps(catalog))
        with self.assertRaises(WorkspaceError):
            list_workspaces(self.path, 'owner@example.com', None)

    def test_untrusted_configuration_permissions_are_rejected(self):
        self.path.chmod(0o666)
        with self.assertRaises(WorkspaceError):
            _config(self.path)

    def test_connection_catalog_path_must_be_absolute(self):
        self.config['connections_path'] = 'connections.json'
        self.save()
        with self.assertRaises(WorkspaceError):
            _config(self.path)

    def test_existing_serve_route_is_never_overwritten(self):
        existing = {'TCP':{'18991':{'HTTPS':True}}, 'Web':{
            'dalton.example.ts.net:18991':{'Handlers':{'/':{'Proxy':'http://127.0.0.1:9999'}}}}}
        with patch('dalton_core.workspace_manager.subprocess.run', return_value=subprocess.CompletedProcess(
                [], 0, json.dumps(existing), '')) as run:
            with self.assertRaises(WorkspaceError):
                _serve(self.config, 18991)
        self.assertEqual(run.call_count, 1)

    def test_config_path_is_explicit_and_optional(self):
        raw = {key:str(self.root / key) for key in ('core_db','state_dir','heartbeat_path','scheduler_db','journal_path')}
        self.assertIsNone(CockpitConfig.from_mapping(raw).workspace_manager_config_path)
        raw['workspace_manager_config_path'] = str(self.path)
        self.assertEqual(CockpitConfig.from_mapping(raw).workspace_manager_config_path, self.path)

    def test_real_blank_provision_renders_only_configured_roles_and_retries_idempotently(self):
        from dalton_core.workspace_release import install_release

        wheel = self.root / "dalton-test-0.1-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("dalton_core/__init__.py", "VERSION='test'\n")
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest()

        def installer(_wheel, venv):
            (venv / "bin").mkdir(parents=True)
            for executable in ("python", "dalton-writer", "daltond", "dalton-control"):
                path = venv / "bin" / executable
                path.write_text("stub")
                path.chmod(0o700)
            package = venv / "lib/python3.14/site-packages/dalton_core"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("VERSION='test'\n")

        installed = install_release(self.root / "fleet", wheel, digest, installer=installer)
        self.config["release_path"] = installed["release_path"]
        self.config["release_ref"] = "release:sha256:" + digest
        from dalton_core.store import content_hash
        catalog_body = {"schema_version": "dalton-shared-connection-catalog-0.1",
                        "models": [], "sources": []}
        catalog = {**catalog_body, "content_hash": content_hash(catalog_body)}
        catalog_path = self.root / "connections.json"
        catalog_path.write_text(json.dumps(catalog))
        self.config["connections_path"] = str(catalog_path)
        self.save()
        launch_result = subprocess.CompletedProcess([], 0, "", "")
        with patch("dalton_core.workspace_manager.subprocess.run", return_value=launch_result), \
             patch("dalton_core.workspace_manager._serve"), \
             patch("dalton_core.workspace_manager._readiness"):
            first = create_managed_workspace(
                self.path, "owner@example.com", "Blank Research", "abcdefgh")
            request_dir = Path(self.config["host_root"]) / "creation-requests"
            request_path = next(request_dir.glob("*.json"))
            record = json.loads(request_path.read_text())
            root = Path(self.config["host_root"]) / "workspaces" / record["slug"]
            tokens_before = (root / "state/dalton-core/writer-tokens.json").read_bytes()
            record["status"] = "failed"
            request_path.write_text(json.dumps(record))
            second = create_managed_workspace(
                self.path, "owner@example.com", "Blank Research", "abcdefgh")
        self.assertEqual(first["workspace"]["workspace_id"], second["workspace"]["workspace_id"])
        record = json.loads(next(request_dir.glob("*.json")).read_text())
        # The retry above completed a second bootstrap without rotating tokens.
        self.assertEqual(tokens_before, (root / "state/dalton-core/writer-tokens.json").read_bytes())
        display = json.loads((root / "display.json").read_text())
        self.assertEqual(display["display_name"], "Blank Research")
        self.assertEqual(display["shared_connection_catalog"]["content_hash"],
                         catalog["content_hash"])
        names = {path.name for path in Path(self.config["launch_agents_dir"]).glob("*.plist")}
        self.assertTrue(any(name.endswith(".writer.plist") for name in names))
        self.assertTrue(any(name.endswith(".controller.plist") for name in names))
        self.assertTrue(any(name.endswith(".control.plist") for name in names))
        self.assertFalse(any(name.endswith(".thesis-impact.plist") for name in names))


if __name__ == '__main__':
    unittest.main()
