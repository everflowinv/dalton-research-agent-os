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
        self.calls=[];self.request_ids=[];self.excluded=[]
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
                owner.request_ids.append(kw.get('request_id'))
                response=owner.responses[purpose]
                if isinstance(response,list):response=response.pop(0)
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

    def test_interrupted_brain_resumes_without_repeating_checker(self):
        self.responses[prep.BRAIN_PURPOSE] = CockpitModelError('this request is already running')
        with self.assertRaisesRegex(ValueError, 'already running'):
            self.run_one()
        self.responses[prep.BRAIN_PURPOSE] = REVISION
        self.assertEqual(self.run_one()['status'], 'passed')
        self.assertEqual(self.calls.count(prep.CHECKER_PURPOSE), 1)
        self.assertEqual(self.calls.count('research_localization'), 1)
        self.assertEqual(self.calls.count(prep.BRAIN_PURPOSE), 2)

    def test_restarted_json_stream_accepts_one_complete_object_only(self):
        text = '```json\n{"overall":"unfinished\n```json\n' + json.dumps(STYLE) + '\n```'
        self.assertEqual(prep.parse_stage_output(text, stage='checker'), STYLE)
        self.assertEqual(prep.parse_stage_output('unfinished {"sections":\n' + json.dumps(CHINESE),
                                                stage='draft'), CHINESE)
        with self.assertRaisesRegex(ValueError, 'unique'):
            prep.parse_stage_output(json.dumps(STYLE) + json.dumps({**STYLE, 'overall':'different'}),
                                    stage='checker')
        with self.assertRaisesRegex(ValueError, 'unique'):
            prep.parse_stage_output('{"overall":"unfinished', stage='checker')

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

    def test_changing_only_verifier_reuses_paid_style_and_reverifies(self):
        self.run_one()
        self.args['verifier_config']={'name':'verify-v2'}
        result=self.run_one()
        self.assertEqual(self.calls,[
            'research_localization',prep.CHECKER_PURPOSE,prep.BRAIN_PURPOSE,
            'research_localization_verifier','research_localization_verifier'])
        verifier_ids=[request_id for purpose,request_id in zip(self.calls,self.request_ids)
                      if purpose=='research_localization_verifier']
        self.assertEqual(len(set(verifier_ids)),2)
        self.assertEqual(result['semantic_identity'],prep.semantic_stage_identity(
            result['pipeline_identity'],self.args['verifier_config'],result['revision_hash']))

    def test_style_config_or_source_change_does_not_reuse_style(self):
        self.run_one()
        self.args['checker_config']={'name':'checker-v2'}
        self.run_one()
        changed=copy.deepcopy(SOURCE);changed['version_ref']='screen:2'
        prep.run_chunk((0,0,changed),**self.args)
        self.assertEqual(self.calls.count(prep.CHECKER_PURPOSE),3)
        self.assertEqual(self.calls.count(prep.BRAIN_PURPOSE),3)
        self.assertEqual(self.calls.count('research_localization'),3)

    def test_explicit_legacy_config_migrates_only_completed_style(self):
        old_verifier={'name':'verify-old'}
        old_identity=prep.pipeline_identity(SOURCE,[self.args['draft_config'],old_verifier,
            self.args['checker_config'],self.args['brain_config']])
        legacy_dir=Path(self.temp.name)/'stages';legacy_dir.mkdir()
        legacy={'source_hash':prep.source_content_hash(SOURCE),'pipeline_identity':old_identity,
            'rules_version':prep.FINAL_TEXT_RULES_VERSION,'pipeline_version':prep.PIPELINE_VERSION,
            'draft':{'route_decision_ref':'research_localization','cost_micros':1},
            'draft_localized':CHINESE,
            'checker_call':{'text':json.dumps(STYLE),'route_decision_ref':prep.CHECKER_PURPOSE,
                            'cost_micros':1},
            'brain_call':{'text':json.dumps(REVISION),'route_decision_ref':prep.BRAIN_PURPOSE,
                          'cost_micros':1},
            'language_review':prep.run_language_review(
                dict(SOURCE,sections=CHINESE['sections']),checker=lambda _:STYLE,
                brain=lambda _:REVISION,checker_identity={
                    'provider':prep.CHECKER_PROVIDER,'model':prep.CHECKER_MODEL}),
            'verifier_call':{'text':'must not migrate','route_decision_ref':'old-verifier'}}
        (legacy_dir/(old_identity+'.json')).write_text(json.dumps(legacy))
        self.args['legacy_verifier_config']=old_verifier
        result=self.run_one()
        self.assertEqual(self.calls,['research_localization_verifier'])
        self.assertNotEqual(result['verifier_call']['route_decision_ref'],'old-verifier')
        self.assertEqual(result['migrated_legacy_pipeline_identity'],old_identity)

    def test_reviewed_repair_reuses_checker_and_preserves_both_failed_stages(self):
        repaired=copy.deepcopy(REVISION)
        repaired['sections'][0]['body']='收入是 123 USD。'
        failed={**VERDICT,'verdict':'fail','faithful':False,
                'findings':['修订把 USD 改成美元，未保留来源中的单位写法']}
        self.responses[prep.BRAIN_PURPOSE]=[REVISION,repaired]
        self.responses['research_localization_verifier']=[failed,VERDICT]
        with self.assertRaises(ValueError):self.run_one()
        self.args['repair_reviewed']=True
        result=self.run_one()
        self.assertEqual(self.calls.count(prep.CHECKER_PURPOSE),1)
        self.assertEqual(self.calls.count(prep.BRAIN_PURPOSE),2)
        self.assertEqual(self.calls.count('research_localization_verifier'),2)
        verifier_ids=[request_id for purpose,request_id in zip(self.calls,self.request_ids)
                      if purpose=='research_localization_verifier']
        self.assertEqual(len(set(verifier_ids)),2)
        self.assertEqual(result['brain_call']['route_decision_ref'],prep.BRAIN_PURPOSE)
        self.assertEqual(json.loads(result['brain_call']['text']),repaired)
        self.assertEqual(result['language_review']['brain_revision'],repaired)
        self.assertEqual(result['localized']['sections'],repaired['sections'])
        self.assertEqual(result['total_cost_micros'],6)
        self.assertEqual([row['stage'] for row in result['review_history']],
                         ['semantic','brain_repair'])
        self.assertIn('verifier_call',result['review_history'][0])
        self.assertEqual(result['review_history'][1]['prior_language_review']['status'],
                         'ready_for_publication')
        self.assertEqual(len(list(Path(self.temp.name).glob('semantic-stages/*.json'))),2)

    def test_reviewed_repair_cannot_publish_an_unfixed_number_change(self):
        bad=copy.deepcopy(REVISION);bad['sections'][0]['body']='收入为 124 美元。'
        self.responses[prep.BRAIN_PURPOSE]=[copy.deepcopy(bad) for _ in range(3)]
        self.args['repair_reviewed']=True
        with self.assertRaisesRegex(ValueError,'number tokens'):self.run_one()
        self.assertEqual(self.calls.count(prep.CHECKER_PURPOSE),1)
        self.assertEqual(self.calls.count(prep.BRAIN_PURPOSE),3)
        self.assertEqual(self.calls.count('research_localization_verifier'),0)
        self.assertFalse(list(Path(self.temp.name).glob('chunks/*.json')))

    def test_reviewed_repair_reuses_a_legacy_failed_semantic_call(self):
        repaired=copy.deepcopy(REVISION);repaired['sections'][0]['body']='收入是 123 USD。'
        failed={**VERDICT,'verdict':'fail','faithful':False,'findings':['单位表达不一致']}
        self.responses[prep.BRAIN_PURPOSE]=[REVISION,repaired]
        self.responses['research_localization_verifier']=[failed,VERDICT]
        with self.assertRaises(ValueError):self.run_one()
        semantic=next(Path(self.temp.name).glob('semantic-stages/*.json'))
        row=json.loads(semantic.read_text())
        legacy=prep.semantic_stage_identity(row['pipeline_identity'],self.args['verifier_config'])
        row['semantic_identity']=legacy;row.pop('revision_hash')
        legacy_path=semantic.parent/(legacy+'.json');legacy_path.write_text(json.dumps(row));semantic.unlink()
        self.args['repair_reviewed']=True
        result=self.run_one()
        self.assertEqual(result['status'],'passed')
        self.assertEqual(self.calls.count(prep.CHECKER_PURPOSE),1)
        self.assertEqual(self.calls.count(prep.BRAIN_PURPOSE),2)
        self.assertEqual(self.calls.count('research_localization_verifier'),2)
