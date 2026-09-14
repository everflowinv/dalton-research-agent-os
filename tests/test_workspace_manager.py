from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dalton_core.workspace import WorkspaceError
from dalton_core.workspace_manager import _config, _serve, list_workspaces, request_create
from dalton_core.cockpit_plane import CockpitConfig


class WorkspaceManagerTests(unittest.TestCase):
    def setUp(self):
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
                name='研究', status='running', url='https://dalton.example.ts.net:18991/', token='secret')))
        result = list_workspaces(self.path, 'owner@example.com', '0')
        self.assertEqual(len(result['items']), 1)
        self.assertTrue(result['items'][0]['current'])
        self.assertNotIn('secret', str(result))

    def test_untrusted_configuration_permissions_are_rejected(self):
        self.path.chmod(0o666)
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


if __name__ == '__main__':
    unittest.main()
