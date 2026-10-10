"""The retired Agenda pin cannot represent or edit an active model default."""
import json
from dataclasses import replace
from tests import test_model_selection as fixture
from tests.test_cockpit_plane import CockpitHarness
from dalton_core.cockpit_plane import CockpitPlane
from dalton_core.model_selection import set_tier_selection, set_model_selection, ModelSelectionError

class RetiredAgendaModelPageTests(fixture.StateDirectoryCase):
    def test_actual_models_page_reads_live_default_and_keeps_agenda_read_only(self):
        state=self.root/'install'/'state'/'dalton-core';state.mkdir(parents=True)
        config=state/self.config_path.name;config.write_bytes(self.config_path.read_bytes())
        service=self.root/'install'/'config'/'service.json';service.parent.mkdir()
        original={'model_router_db':str(self.router.path) if hasattr(self.router,'path') else str(self.root/'model-router.sqlite'),
                  'agenda':{'enabled':True,'config':{'routing_policy_ref':self.policies['brain'],'model_router_db':str(self.root/'model-router.sqlite')}}}
        service.write_text(json.dumps(original))
        (self.root/'cockpit').mkdir();c=CockpitHarness(self.root/'cockpit');self.addCleanup(c.close)
        cfg=replace(c.config,state_dir=state,model_config_path=config)
        plane=CockpitPlane(cfg,writer_socket=state/'w.sock',token_config=state/'t.json',
                           governance_call=lambda *a,**k:self.fail('read called governance'),clock=lambda:fixture.NOW)
        self.addCleanup(plane.close)
        save=set_tier_selection(state,tier='brain',mode='explicit',chain=['profile:claude-fable-5-1'],now=fixture.NOW)
        self.assertNotIn('agenda_planning',save['purposes'])
        self.assertEqual(json.loads(service.read_text()),original)
        view=plane.models();self.assertTrue(view['available'])
        archived=next(row for row in view['purposes'] if row['purpose']=='agenda_planning')
        self.assertEqual(archived['retired_by'],'ADR-0009');self.assertFalse(archived['editable'])
        card=next(row for row in view['tier_cards'] if row['tier']=='brain')
        self.assertEqual([link['model'] for link in card['chain']],['profile:claude-fable-5-1'])
        self.assertNotIn('历史任务规划',json.dumps(card,ensure_ascii=False))
        before=config.read_bytes();count=self.router.connection.execute('SELECT COUNT(*) FROM model_routing_policy_versions').fetchone()[0]
        with self.assertRaisesRegex(ModelSelectionError,'ADR-0009'):
            set_model_selection(state,purpose='agenda_planning',mode='explicit',chain=['profile:gpt-6-astra'],now=fixture.NOW)
        self.assertEqual(config.read_bytes(),before);self.assertEqual(json.loads(service.read_text()),original)
        self.assertEqual(self.router.connection.execute('SELECT COUNT(*) FROM model_routing_policy_versions').fetchone()[0],count)
