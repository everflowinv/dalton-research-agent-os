from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from dalton_core.workspace import create_workspace_manifest, load_workspace_manifest, WorkspaceError
from dalton_core.bootstrap import bootstrap
from dalton_core.workspace_lifecycle import transition, archive_integrity, state_for_core, readonly_research
from dalton_core.workspace_manager import _write, _port, list_workspaces, create_managed_workspace
from dalton_core.workspace_fleet import fleet_overview


class WorkspaceLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.host = self.root / 'host'; self.release = self.root / 'release'; self.release.mkdir()
        self.agents = self.root / 'agents'; self.agents.mkdir()
        self.config_path = self.root / 'manager.json'
        self.config = {'host_root': str(self.host), 'release_path': str(self.release),
                       'release_ref': 'release:sha256:' + 'a' * 64, 'owner_login': 'owner@example.com',
                       'allowed_tailscale_logins': ['owner@example.com','analyst@example.com'], 'admin_logins': ['owner@example.com'],
                       'tailscale_host': 'test.example.ts.net', 'tailscale_executable': '/bin/false',
                       'launch_agents_dir': str(self.agents), 'ports': [19271,19272,19273]}
        _write(self.config_path, self.config)
        self.slug = 'ws-' + 'a' * 24
        self.workspace = create_workspace_manifest(self.host, self.slug, 19271, self.config['release_ref'], self.release)
        bootstrap(self.workspace.state_dir, self.workspace.config_path, workspace_manifest=self.workspace.manifest_path)
        self.record = self.host / 'creation-requests' / ('a'*24 + '.json')
        _write(self.record, {'name':'项目甲','slug':self.slug,'owner_login':'analyst@example.com', 'workspace_id':self.workspace.workspace_id,
                             'port':19271,'status':'running','lifecycle':'active','created_at':datetime.now(timezone.utc).isoformat()})
        self.run = lambda args, **kwargs: subprocess.CompletedProcess(args, 0, '{}', '')

    def change(self, action, login='owner@example.com', **kwargs):
        body = {'slug':self.slug, 'action':action, 'request_id':action + '_request_001', 'reason':'测试有界操作', **kwargs}
        return transition(self.config_path, login, body, run=self.run)

    def test_full_lifecycle_preserves_exact_state_restores_and_deletes_history_retained(self):
        core = self.workspace.state_dir / 'core.sqlite'
        before = hashlib.sha256(core.read_bytes()).hexdigest()

        state = self.change('pause', login='analyst@example.com')
        self.assertEqual(state['status'], 'paused')
        self.assertTrue(self.change('pause', login='analyst@example.com')['replayed'])
        self.assertEqual(_port(self.config, [json.loads(self.record.read_text())]), 19272)
        paths_before = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in self.workspace.state_dir.rglob('*') if path.is_file()}
        self.assertTrue(readonly_research(self.config_path,'analyst@example.com',self.slug)['read_only'])
        paths_after = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in self.workspace.state_dir.rglob('*') if path.is_file()}
        self.assertEqual(paths_before,paths_after)
        self.change('archive')
        self.assertFalse(core.exists())
        proof = archive_integrity(self.host,self.slug); self.assertEqual(proof['status'],'verified')
        self.assertEqual(_port(self.config,[json.loads(self.record.read_text())]),19271)
        with patch('dalton_core.workspace_process.install_workspace'), patch('dalton_core.workspace_process._run_launchctl'), patch('dalton_core.workspace_manager._serve'), patch('dalton_core.workspace_manager._readiness'):
            self.change('restore')
        self.assertEqual(hashlib.sha256(core.read_bytes()).hexdigest(), before)
        self.assertEqual(load_workspace_manifest(self.workspace.manifest_path).workspace_id, self.workspace.workspace_id)
        self.change('pause',request_id='pause_second_001')
        with self.assertRaises(PermissionError): self.change('delete',login='analyst@example.com',confirm_name='项目甲',irreversible=True,review_ack=True)
        self.change('delete',confirm_name='项目甲',irreversible=True,review_ack=True)
        self.assertFalse(core.exists()); self.assertFalse(Path(proof['archive_path']).exists())
        self.assertEqual(load_workspace_manifest(self.workspace.manifest_path).lifecycle,'deleted')
        db = sqlite3.connect(self.host/'workspace-lifecycle.sqlite3')
        try:
            events = db.execute('SELECT action,actor,reason FROM workspace_lifecycle_events').fetchall()
            self.assertIn(('delete','owner@example.com','测试有界操作'),events)
            with self.assertRaises(sqlite3.IntegrityError): db.execute('DELETE FROM workspace_lifecycle_events')
            with self.assertRaises(sqlite3.IntegrityError): db.execute("UPDATE workspace_lifecycle_events SET reason='changed'")
        finally: db.close()

    def test_primary_and_active_deletion_refused_before_process_action(self):
        with patch('dalton_core.workspace_process._run_launchctl') as start:
            with self.assertRaises(WorkspaceError): transition(self.config_path,'owner@example.com',{'slug':'legacy','action':'pause','request_id':'test_request','reason':'test'})
            with self.assertRaises(WorkspaceError): self.change('delete',confirm_name='项目甲',irreversible=True,review_ack=True)
            start.assert_not_called()

    def test_external_namespace_logs_archive_restore_delete_and_peer_preserved(self):
        logs = self.host / 'workspace-logs' / self.slug
        logs.mkdir(parents=True); (logs / 'writer.log').write_bytes(b'exact old log\n')
        peer = logs.parent / ('ws-' + 'b' * 24)
        peer.mkdir(); (peer / 'shared.log').write_bytes(b'peer')
        self.change('pause'); self.change('archive')
        proof = archive_integrity(self.host, self.slug)
        self.assertEqual(proof['status'], 'verified')
        self.assertTrue(Path(proof['log_archive_path']).is_file())
        self.assertFalse(logs.exists()); self.assertEqual((peer / 'shared.log').read_bytes(), b'peer')
        with patch('dalton_core.workspace_process.install_workspace'), patch('dalton_core.workspace_process._run_launchctl'), patch('dalton_core.workspace_manager._serve'), patch('dalton_core.workspace_manager._readiness'):
            self.change('restore')
        self.assertEqual((logs / 'writer.log').read_bytes(), b'exact old log\n')
        self.change('pause', request_id='pause_second_001')
        self.change('delete', confirm_name='项目甲', irreversible=True, review_ack=True)
        self.assertFalse(logs.exists()); self.assertFalse(Path(proof['log_archive_path']).exists())
        self.assertEqual((peer / 'shared.log').read_bytes(), b'peer')

    def test_namespace_and_shared_log_symlinks_refuse_before_removing_state(self):
        self.change('pause')
        outside = self.root / 'outside'; outside.mkdir(); (outside / 'keep').write_text('shared')
        parent = self.host / 'workspace-logs'; parent.mkdir()
        for linked in (parent / self.slug, parent):
            if linked == parent: parent.rmdir()
            linked.symlink_to(outside, target_is_directory=True)
            for action in ('archive', 'delete'):
                with self.subTest(linked=linked, action=action), self.assertRaises(WorkspaceError):
                    self.change(action, confirm_name='项目甲', irreversible=True, review_ack=True)
                self.assertTrue((self.workspace.state_dir / 'core.sqlite').exists())
                self.assertEqual((outside / 'keep').read_text(), 'shared')
            linked.unlink()

    def test_log_archive_tamper_refuses_restore_before_process_start(self):
        logs = self.host / 'workspace-logs' / self.slug
        logs.mkdir(parents=True); (logs / 'control.log').write_text('original')
        self.change('pause'); self.change('archive')
        Path(archive_integrity(self.host, self.slug)['log_archive_path']).write_bytes(b'tampered')
        with patch('dalton_core.workspace_process._run_launchctl') as process:
            with self.assertRaises(WorkspaceError): self.change('restore')
            process.assert_not_called()
        self.assertFalse(logs.exists())

    def test_workspaces_parent_on_external_volume_is_cleaned_through_registry_link(self):
        registry = self.host / 'workspaces'
        volume = self.root / 'ssd-workspaces'
        registry.rename(volume); registry.symlink_to(volume, target_is_directory=True)
        self.change('pause')
        self.change('delete', confirm_name='项目甲', irreversible=True, review_ack=True)
        self.assertFalse((volume / self.slug / 'state').exists())
        self.assertTrue((volume / self.slug / 'workspace.json').is_file())
        self.assertTrue(registry.is_symlink())

    def test_confirmation_rejection_preserves_bytes_and_pending_review_visible(self):
        self.change('pause')
        draft = self.workspace.state_dir / 'creation-goal-draft.json'; _write(draft,{'objective':'待补充'})
        listed = list_workspaces(self.config_path,'owner@example.com',None)
        self.assertEqual(listed['items'][0]['pending_review'][0]['table'],'unpublished_goal')
        for fields in ({'confirm_name':'wrong','irreversible':True,'review_ack':True},
                       {'confirm_name':'项目甲','irreversible':False,'review_ack':True},
                       {'confirm_name':'项目甲','irreversible':True,'review_ack':False}):
            with self.subTest(fields=fields), self.assertRaises(WorkspaceError): self.change('delete',**fields)
            self.assertTrue(draft.exists())
        self.assertEqual(state_for_core(self.workspace.state_dir),'paused')

    def test_bad_archive_never_extracts_or_starts(self):
        self.change('pause'); self.change('archive')
        path = Path(archive_integrity(self.host,self.slug)['archive_path']); path.write_bytes(b'tampered')
        with patch('dalton_core.workspace_process._run_launchctl') as process:
            with self.assertRaises(WorkspaceError): self.change('restore')
            process.assert_not_called()
        self.assertEqual(state_for_core(self.workspace.state_dir),'archived')

    def test_restore_does_not_steal_released_port(self):
        self.change('pause'); self.change('archive')
        create_workspace_manifest(self.host,'ws-'+'b'*24,19271,self.config['release_ref'],self.release)
        with patch('dalton_core.workspace_process.install_workspace'), patch('dalton_core.workspace_process._run_launchctl'), patch('dalton_core.workspace_manager._serve'), patch('dalton_core.workspace_manager._readiness'):
            self.change('restore')
        self.assertEqual(load_workspace_manifest(self.workspace.manifest_path).cockpit_port,19272)
        overview = fleet_overview(self.host)
        self.assertNotIn('invalid',[row['state'] for row in overview['workspaces']])
        self.assertEqual(json.loads(self.record.read_text())['port'],19272)

    def test_model_sync_and_parity_exclude_stopped_workspaces(self):
        self.change('pause')
        from dalton_core.model_routing_sync import host_environments
        self.assertEqual(host_environments(self.config_path), [])
        from dalton_core.workspace_lane_parity import plan_parity_actions, audit_lanes
        self.assertEqual(plan_parity_actions(self.workspace.state_dir,actor_ref='human:owner',host_sources={}),[])
        self.assertEqual(audit_lanes(self.workspace.state_dir,host_sources={})['status'],'intentionally_stopped')
        from dalton_core.workspace_health_parity import Environment, check_environment
        env = Environment(self.slug,self.workspace.state_dir,service_config=self.workspace.config_path)
        try: self.assertEqual(check_environment(env)['lifecycle'],'paused')
        finally: env.close()

    def test_allowed_analyst_creation_and_daily_quota_are_idempotent(self):
        def install(manifest, directory):
            workspace = load_workspace_manifest(manifest)
            from dalton_core.workspace_process import label_namespace
            for role in ('writer','controller','control'):
                (Path(directory)/f'{label_namespace(workspace.slug)}.{role}.plist').write_text('synthetic')
        with patch('dalton_core.workspace_manager._readiness'), patch('dalton_core.workspace_manager._serve'), patch('dalton_core.workspace_process.install_workspace',side_effect=install), patch('dalton_core.workspace_manager.subprocess.run',self.run), patch('dalton_core.workspace_creation.sys.platform','test'):
            for index in range(2):
                create_managed_workspace(self.config_path,'analyst@example.com',f'项目{index}',f'create_request_{index}')
            self.assertEqual(list_workspaces(self.config_path,'analyst@example.com',None)['quota']['used'],3)
            with self.assertRaisesRegex(WorkspaceError,'配额'):
                create_managed_workspace(self.config_path,'analyst@example.com','第4','create_request_four')
            result = create_managed_workspace(self.config_path,'analyst@example.com','项目0','create_request_0')
            self.assertEqual(result['status'],'running')
            with self.assertRaises(PermissionError): create_managed_workspace(self.config_path,'stranger@example.com','未知','create_request_stranger')
        self.assertEqual(list_workspaces(self.config_path,'analyst@example.com',None)['can_create'],False)

    def test_empty_seven_days_has_lifecycle_actions_and_keep_resets_age(self):
        from dalton_core.needs_human import workspaces_without_mission, workspace_probes
        record = json.loads(self.record.read_text()); record['created_at'] = (datetime.now(timezone.utc)-timedelta(days=8)).isoformat(); _write(self.record,record)
        items = workspaces_without_mission(workspace_probes(self.config_path))
        self.assertEqual(items[0]['kind'],'empty_workspace'); self.assertEqual(items[0]['detail']['age_days'],8)
        self.assertEqual(items[0]['detail']['actions'],['pause','delete','keep'])
        self.change('keep')
        self.assertEqual(workspaces_without_mission(workspace_probes(self.config_path))[0]['kind'],'no_active_mission')
        self.change('pause')
        self.assertEqual(workspaces_without_mission(workspace_probes(self.config_path)),[])

    def test_initial_goal_publishes_real_v1_with_bounded_defaults_without_model_or_network(self):
        from tests.test_workspace_mission_setup import WorkspaceFirstMissionTests
        from tests.p9a_fixtures import bootstrap_method_authorities, constitution_method
        from dalton_core.store import DaltonStore, content_hash
        from dalton_core.workspace_manager import _initial_goal
        fixture = WorkspaceFirstMissionTests(); fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        store = DaltonStore(str(self.workspace.state_dir / 'core.sqlite'))
        seeded = bootstrap_method_authorities(store)
        pack = seeded['pack']
        driver = {key: pack[key] for key in ('drivers','metric_specs','thesis_templates')}
        body = {key: value for key,value in fixture.foundation.items() if key != 'content_hash'}
        body['workspace_id'] = self.workspace.workspace_id
        body['methods'] = {'playbook': {'binding': {'ref':seeded['playbook']['id'],'hash':seeded['playbook']['content_hash']}},
                           'driver_pack_template': {'value':driver,'content_hash':content_hash(driver)},
                           'constitution_method': {'value':constitution_method(),'content_hash':content_hash(constitution_method())}}
        _write(self.workspace.state_dir / 'research-foundation.json',{**body,'content_hash':content_hash(body)})
        store.close()
        with patch('dalton_core.workspace_mission_setup.resolve_sec_ticker') as network, patch('dalton_core.cockpit_model.CockpitModel.call_setup') as model:
            status = _initial_goal(self.workspace,'研究半导体设备行业，比较 $ASML。每天最多花 $999。','analyst@example.com')
            self.assertEqual(status,'published_v1'); network.assert_not_called(); model.assert_not_called()
        db = sqlite3.connect(self.workspace.state_dir/'core.sqlite')
        try:
            row = db.execute('SELECT version_number,record_json FROM coverage_mission_versions ORDER BY version_number DESC LIMIT 1').fetchone()
            self.assertEqual(row[0],1); mission = json.loads(row[1])
            self.assertEqual(mission['budget']['max_daily_cost_usd'],3.0)
            self.assertEqual([item['ticker'] for item in mission['universe']],['ASML'])
        finally: db.close()
        self.change('pause')
        with patch('dalton_core.cockpit_research_library.research_library',return_value={'products':[{'status':'available','version_ref':'report:one','label':'已有研究','sections':[{'title':'结论','body':'保存的正文'}]}]}) as read:
            view = readonly_research(self.config_path,'analyst@example.com',self.slug)
        read.assert_called_once(); self.assertEqual(view['products'][0]['sections'][0]['body'],'保存的正文')

    def test_incomplete_initial_goal_is_saved_for_review_without_invented_scope(self):
        from dalton_core.workspace_manager import _initial_goal
        self.assertEqual(_initial_goal(self.workspace,'看看未来机会','analyst@example.com'),'review_required')
        draft = json.loads((self.workspace.state_dir/'creation-goal-draft.json').read_text())
        self.assertEqual(draft['objective'],'看看未来机会')
        db = sqlite3.connect(self.workspace.state_dir/'cockpit'/'journal.sqlite')
        try:
            row = db.execute("SELECT input_text,status FROM cockpit_drafts WHERE kind='goal'").fetchone()
            self.assertEqual(row,('看看未来机会','open'))
        finally: db.close()

    def test_release_plan_excludes_stopped_agents_and_manifests(self):
        import argparse, plistlib
        script = Path(__file__).resolve().parents[1] / 'scripts' / 'release_switch.py'
        spec = importlib.util.spec_from_file_location('az_release_switch',script)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        self.change('pause')
        from dalton_core.workspace_process import label_namespace
        for role in ('writer','controller','control'):
            with (self.agents/f'{label_namespace(self.slug)}.{role}.plist').open('wb') as stream:
                plistlib.dump({'Label':f'{label_namespace(self.slug)}.{role}','ProgramArguments':['/old/python'],'StandardErrorPath':'/tmp/error'},stream)
        with (self.agents/'space.lumos.dalton.primary.writer.plist').open('wb') as stream:
            plistlib.dump({'Label':'space.lumos.dalton.primary.writer','ProgramArguments':['/old/python'],'StandardErrorPath':'/tmp/error'},stream)
        candidate = self.root / 'runtime' / 'releases' / ('b'*64)
        python = candidate/'venv'/'bin'/'python'; python.parent.mkdir(parents=True); python.write_text('synthetic'); python.chmod(0o700)
        args = argparse.Namespace(release=candidate,source_commit='c'*40,candidate_manifest_sha256='d'*64,
                                  launch_agents_dir=self.agents,manager=self.config_path,host_root=self.host,
                                  worker_config=self.root/'absent-worker.json',release_pointer=self.root/'release-pointer.json',runtime_pointer=self.root/'runtime-pointer.json')
        plan = module.build_plan(args)
        self.assertEqual(len(plan['skipped_launch_agents']),3)
        self.assertFalse(plan['workspaces'][0]['needs_change']); self.assertTrue(plan['workspaces'][0]['skipped'])
        self.assertFalse(any(self.slug in row['label'] for row in plan['launch_agents']))
        # Retained paused plists still point at their old release by design.
        # The post-switch check must use the same active set as the switch.
        primary = self.agents / 'space.lumos.dalton.primary.writer.plist'
        with primary.open('wb') as stream:
            plistlib.dump({'Label': primary.stem, 'ProgramArguments': [str(python)]}, stream)
        manager = json.loads(self.config_path.read_text())
        manager['release_ref'] = plan['release_ref']; _write(self.config_path, manager)
        args.state_dir = self.workspace.state_dir
        args.writer_socket = self.workspace.state_dir / 'run/writer.sock'
        args.heartbeat_wait_seconds = 0
        with patch.object(module, 'controller_heartbeat', side_effect=['01', '02']), patch.object(
            module, 'writer_socket_readable', return_value=True
        ):
            health = module.health(args, plan)
        self.assertTrue(health['ok'])
        self.assertEqual(len(health['agents']), 1)
        self.assertEqual(len(health['intentionally_stopped_agents']), 3)

    def test_detached_control_request_validates_then_survives_bootout_namespace(self):
        from dalton_core.workspace_lifecycle import request_transition
        value = {'slug':self.slug,'action':'pause','request_id':'pause_detached_01','reason':'停止研究'}
        with patch.dict(os.environ,{'DALTON_WORKSPACE_MANIFEST':'/other/workspace.json'}), patch('dalton_core.workspace_lifecycle.subprocess.Popen') as child:
            result = request_transition(self.config_path,'analyst@example.com',value)
        self.assertEqual(result['status'],'accepted')
        self.assertTrue(child.call_args.kwargs['start_new_session'])
        self.assertNotIn('DALTON_WORKSPACE_MANIFEST',child.call_args.kwargs['env'])
        self.assertEqual(state_for_core(self.workspace.state_dir),'active')
        self.assertEqual(json.loads(child.call_args.args[0][-1]),value)

    def test_paused_start_publication_weekly_and_stale_parity_plan_cannot_restart(self):
        self.change('pause')
        from dalton_core.workspace_process import _run_launchctl
        with self.assertRaises(WorkspaceError):
            _run_launchctl(self.workspace.manifest_path,self.agents,'start',uid=os.getuid(),run=self.run)
        from dalton_core.research_publication_authority import published_runtime_gate
        gate = published_runtime_gate({'workspace_manifest':str(self.workspace.manifest_path)},prefix=str(self.release))
        self.assertEqual(gate['status'],'workspace_intentionally_stopped'); self.assertEqual(gate['model_calls'],0)
        from dalton_core.weekly_brief_coordinator import run_weekly_brief_cycle
        from types import SimpleNamespace
        result = run_weekly_brief_cycle(SimpleNamespace(path=str(self.workspace.state_dir/'core.sqlite')),None,None,plan={},as_of='2026-10-10T00:00:00+00:00',actor_ref='core')
        self.assertEqual(result['status'],'skipped')
        from dalton_core.workspace_lane_parity import apply_parity_actions, ParityAction, LaneParityError
        with self.assertRaises(LaneParityError):
            apply_parity_actions([ParityAction(kind='link',target=str(self.workspace.state_dir/'new.json'),detail='/tmp/input',reason='fixture')],actor_ref='human:owner')

    def test_readonly_scope_rejects_other_allowed_creator(self):
        self.change('pause')
        self.config['allowed_tailscale_logins'].append('other@example.com'); _write(self.config_path,self.config)
        with self.assertRaises(PermissionError): readonly_research(self.config_path,'other@example.com',self.slug)
        with self.assertRaises(PermissionError): self.change('restore',login='other@example.com')

if __name__ == '__main__': unittest.main()
