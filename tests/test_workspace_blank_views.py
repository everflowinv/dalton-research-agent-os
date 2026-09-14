import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from dalton_core.cockpit_plane import CockpitConfig, CockpitPlane, CockpitConflict
from dalton_core.workspace_creation import create_blank_workspace
from dalton_core.store import content_hash


class BlankViewsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        release = self.root / 'release'
        release.mkdir()
        catalog = {'schema_version':'dalton-shared-connection-catalog-0.1', 'models':[
            dict(id='model:test', provider='openai', model='test-model', family='openai-gpt-5.6',
                 adapter_ref='adapter:test', credential_slot_ref='credential:test',
                 capabilities=['research'],modalities=['text'],
                 transport=dict(kind='broker',endpoint_ref='broker:test',socket_path=None,config_path=None))],
            'sources':[dict(id='connector:sec:1', connector_ref='connector:sec', capability_id='sec-read',
                auth_mode='public',credential_slot_refs=[],allowed_operations=['read'],allowed_hosts=['sec.gov'],
                transport=dict(kind='connector',endpoint_ref='adapter:sec',socket_path=None,config_path=None))]}
        catalog['content_hash'] = content_hash(catalog)
        catalog_path = self.root / 'connections.json'
        catalog_path.write_text(json.dumps(catalog))
        receipt = create_blank_workspace(self.root, 'new', 18951, 'release:sha256:'+'a'*64, release,
            request_id='creation-test', display_name='新研究', shared_readonly_paths=[catalog_path],
            connection_catalog=dict(path=str(catalog_path),content_hash=catalog['content_hash']))
        self.manifest = receipt['manifest_path']
        env = patch.dict(os.environ, DALTON_WORKSPACE_MANIFEST=self.manifest)
        env.start()
        self.addCleanup(env.stop)
        state = self.root/'workspaces/new/state/dalton-core'
        config = CockpitConfig(core_db=state/'core.sqlite',state_dir=state,
            heartbeat_path=state/'run/heartbeat.json',scheduler_db=state/'scheduler.sqlite',
            journal_path=state/'cockpit/journal.sqlite')
        self.plane = CockpitPlane(config, writer_socket=state/'run/writer.sock',token_config=state/'writer-tokens.json')
        self.addCleanup(self.plane.close)

    def test_blank_views_show_catalog_without_research_authority(self):
        overview = self.plane.overview()
        self.assertEqual(overview['workspace']['name'], '新研究')
        self.assertEqual(overview['state'], 'awaiting_mission')
        models = self.plane.models()
        self.assertFalse(models['available'])
        self.assertNotEqual(models['shared_catalog']['models'][0]['display_name'], '未登记模型')
        self.assertFalse(models['shared_catalog']['workspace_authorized'])
        sources = self.plane.sources()
        self.assertEqual(sources['sources'][0]['mission']['status'], 'undeclared')
        self.assertNotIn('socket_path', json.dumps(models))
        self.assertNotIn('credential_slot', json.dumps(sources))

    def test_empty_research_pages_are_readable(self):
        self.assertEqual(self.plane.approvals()['count'], 0)
        self.assertEqual(self.plane.log()['events'], [])
        self.assertEqual(self.plane.claims()['total'], 0)
        self.assertFalse(self.plane.cycle_reflection()['available'])

    def test_first_goal_is_saved_once_without_model_or_mission(self):
        with patch.object(self.plane, '_model_instance', side_effect=AssertionError('no model calls')):
            one = self.plane._initial_goal_draft('owner@example.com', '研究新的行业', 'test-request')
            two = self.plane._initial_goal_draft('owner@example.com', '研究新的行业', 'test-request')
        self.assertEqual(one['draft_id'], two['draft_id'])
        self.assertEqual(one['cost_usd'], 0)
        self.assertFalse(one['draft']['research_authorized'])
        self.assertEqual(self.plane.overview()['state'], 'awaiting_mission')
        rows = self.plane.journal.rows('SELECT status FROM cockpit_drafts')
        self.assertEqual([r['status'] for r in rows], ['saved'])
        with self.assertRaises(CockpitConflict):
            self.plane._initial_goal_draft('owner@example.com', '另一份目标', 'test-request')


if __name__ == '__main__':
    unittest.main()
