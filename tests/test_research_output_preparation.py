from __future__ import annotations
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dalton_core import research_output_preparation as prep
from dalton_core.cockpit_model import CockpitModelError

SOURCE={'kind':'initial_screen','version_ref':'screen:1','status':'available',
        'sections':[{'title':'Revenue','body':'Revenue was 123 USD.','gaps':[]}]}
CHINESE={'sections':[{'index':0,'title':'收入','body':'收入为 123 USD。','gaps':[]}]}
STYLE={'overall':'表达清楚，可略作精简。','suggestions':[{'section_index':0,'quote':'收入为 123 USD。',
        'assessment':'币种可直接用中文。','suggestion':'收入为 123 美元。'}]}
REVISION={'decisions':[{'suggestion_index':0,'decision':'adopt','reason':'币种中文更易读。'}],
          'sections':[{'index':0,'title':'收入','body':'收入为 123 美元。','gaps':[]}]}
VERDICT={'verdict':'pass','faithful':True,'no_new_facts':True,'meaning_preserved':True,'findings':[]}

class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.calls=[];self.excluded=[]
        self.args=dict(mission={},draft_config={'name':'draft'},verifier_config={'name':'verify'},
          checker_config={'name':'checker'},brain_config={'name':'brain'},scheduler_db=Path('unused'),
          work_dir=Path(self.temp.name),max_cost=.2,attempts=3)
        self.responses={'research_localization':CHINESE,prep.CHECKER_PURPOSE:STYLE,
                        prep.BRAIN_PURPOSE:REVISION,'research_localization_verifier':VERDICT}
        owner=self
        class Model:
            def __init__(self,*a,**kw):pass
            def call(self,*,purpose,**kw):
                owner.calls.append(purpose)
                response=owner.responses[purpose]
                if isinstance(response,Exception):raise response
                return {'text':json.dumps(response),'route_decision_ref':purpose,'cost_micros':1}
        def independent(model,*,producer_route_decision_refs,**kw):
            owner.excluded.append(producer_route_decision_refs)
            return model.call(**kw)
        self.enterContext(patch.object(prep,'CockpitModel',Model))
        self.enterContext(patch.object(prep,'independent_model_call',independent))
        self.enterContext(patch.object(prep,'selected_identity',return_value={
            'provider':prep.CHECKER_PROVIDER,'model':prep.CHECKER_MODEL}))
        self.enterContext(patch.object(prep,'router_family_resolver',return_value=lambda ref:ref))

    def run_one(self):return prep.run_chunk((0,0,SOURCE),**self.args)[2]

    def test_checker_then_brain_then_semantic_verifier_replays_without_new_calls(self):
        result=self.run_one()
        self.assertEqual(self.calls,['research_localization',prep.CHECKER_PURPOSE,prep.BRAIN_PURPOSE,
                                     'research_localization_verifier'])
        self.assertEqual(self.excluded,[['research_localization',prep.BRAIN_PURPOSE]])
        self.assertEqual(result['localized']['sections'][0]['body'],'收入为 123 美元。')
        self.assertEqual(len(list(Path(self.temp.name).glob('language-reviews/*.md'))),1)
        self.assertEqual(self.run_one(),result)
        self.assertEqual(len(self.calls),4)

    def test_route_failure_does_not_redraft_or_call_checker(self):
        self.responses['research_localization']=CockpitModelError('model unavailable')
        with self.assertRaises(CockpitModelError):self.run_one()
        self.assertEqual(self.calls,['research_localization'])

    def test_brain_number_change_cannot_reach_verifier_or_publication(self):
        bad=copy.deepcopy(REVISION);bad['sections'][0]['body']='收入为 124 美元。'
        self.responses[prep.BRAIN_PURPOSE]=bad
        with self.assertRaisesRegex(ValueError,'number tokens'):self.run_one()
        with self.assertRaises(ValueError):self.run_one()
        self.assertEqual(self.calls,['research_localization',prep.CHECKER_PURPOSE,prep.BRAIN_PURPOSE])
        self.assertFalse(list(Path(self.temp.name).glob('chunks/*.json')))

    def test_rules_and_config_changes_have_distinct_replay_identity(self):
        old=prep.pipeline_identity(SOURCE,[{'policy':'a'}])
        self.assertNotEqual(old,prep.pipeline_identity(SOURCE,[{'policy':'b'}]))
        with patch.object(prep,'FINAL_TEXT_RULES_VERSION','future'):
            self.assertNotEqual(old,prep.pipeline_identity(SOURCE,[{'policy':'a'}]))
