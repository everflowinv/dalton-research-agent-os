import unittest
from dalton_core.cockpit_model_variants import broker_choice_metadata

class ModelVariantMetadataTests(unittest.TestCase):
    def config(self,rows):
        return {'apiKey':'not-exported','plugins':{'entries':{'dalton-openclaw-model-broker':{'config':{'profiles':rows}}}}}
    def catalogue(self):
        return {'profile:one':{'provider':'gateway','model':'same'},'profile:two':{'provider':'gateway','model':'same'}}
    def test_exact_identity_exposes_only_actual_preset_and_route(self):
        raw=[{'id':'profile:one','model':'gateway/same','thinkingLevel':'low','secret':'not-exported'},
             {'id':'profile:two','model':'gateway/same','thinkingLevel':'high'}]
        result=broker_choice_metadata(self.config(raw),self.catalogue())
        self.assertEqual(result,{'profile:one':{'broker_model_ref':'gateway/same','broker_thinking_preset':'low'},
                                'profile:two':{'broker_model_ref':'gateway/same','broker_thinking_preset':'high'}})
    def test_wrong_model_unregistered_or_conflicting_controls_do_not_claim_variant(self):
        for raw in [{'id':'profile:one','model':'other/same','thinkingLevel':'high'},
                    {'id':'profile:missing','model':'gateway/same','thinkingLevel':'high'},
                    {'id':'profile:one','model':'gateway/same','thinkingLevel':'high','providerControls':{'thinkingLevel':'low'}},
                    {'id':'profile:one','model':'gateway/same','thinkingLevel':'unsupported'}]:
            with self.subTest(raw=raw):self.assertEqual(broker_choice_metadata(self.config([raw]),self.catalogue()),{})
    def test_duplicate_identity_is_ambiguous_not_last_writer_wins(self):
        row={'id':'profile:one','model':'gateway/same','thinkingLevel':'low'}
        self.assertEqual(broker_choice_metadata(self.config([row,row,{**row,'thinkingLevel':'high'}]),self.catalogue()),{})
    def test_missing_preset_is_not_inferred_from_identity_or_model(self):
        catalogue={'profile:high':{'provider':'gateway','model':'same-high'}}
        out=broker_choice_metadata(self.config([{'id':'profile:high','model':'gateway/same-high'}]),catalogue)
        self.assertEqual(out,{'profile:high':{'broker_model_ref':'gateway/same-high'}})
        self.assertEqual(broker_choice_metadata(None,catalogue),{})

    def test_actual_models_read_projection_carries_preset_without_changing_policy(self):
        import dataclasses
        import hashlib
        import json
        import tempfile
        from pathlib import Path
        from tests.test_cockpit_plane import CockpitHarness
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);harness=CockpitHarness(root)
            try:
                initial=harness.plane.models();choice=initial['choices'][0]
                original=harness.model_config_path.read_bytes()
                canonical=choice['provider']+'/'+choice['model_ref']
                config={'models':{'providers':{choice['provider']:{'models':[{
                    'id':choice['model_ref'],'contextWindow':100000,'maxTokens':4096,
                    'cost':{'input':1,'output':2}}]}}},'plugins':{'entries':{}}}
                config['plugins']['entries']['dalton-openclaw-model-broker']={'config':{
                    'profiles':[{'id':choice['model'],'model':canonical,'thinkingLevel':'high'}],
                    'authKey':'must-not-leak'}}
                path=root/'broker.json';path.write_text(json.dumps(config))
                harness.plane.config=dataclasses.replace(harness.plane.config,openclaw_config_path=path)
                result=harness.plane.models();self.assertTrue(result['available'])
                projected=next(c for c in result['choices'] if c['model']==choice['model'])
                self.assertEqual(projected['broker_thinking_preset'],'high')
                self.assertEqual(projected['broker_model_ref'],canonical)
                self.assertNotIn('must-not-leak',json.dumps(result))
                self.assertEqual(original,harness.model_config_path.read_bytes())
                self.assertEqual(harness.calls,[])
            finally:harness.close()
