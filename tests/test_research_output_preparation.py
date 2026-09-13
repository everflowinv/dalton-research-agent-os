from __future__ import annotations
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dalton_core import research_output_preparation as prep
from dalton_core.cockpit_model import CockpitModelError, CockpitModelRouteUnavailable

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

    def test_repair_prompt_keeps_display_facts_without_repeating_authority_payloads(self):
        source = copy.deepcopy(SOURCE)
        source['sources'] = [{'raw_document': 'large-raw-authority' * 10000}]
        prompt = prep._repair_prompt(product=source, draft_localized=CHINESE,
            review={'language_review': STYLE, 'brain_revision': REVISION},
            failure={'reason': 'preserve the currency'}, attempt=1)
        self.assertNotIn('large-raw-authority', prompt)
        self.assertIn('Revenue was 123 USD.', prompt)
        self.assertIn(prep.source_content_hash(source), prompt)
        self.assertIn('preserve the currency', prompt)

    def test_initial_brain_can_reduce_output_only_after_a_proved_route_rejection(self):
        self.responses[prep.BRAIN_PURPOSE] = [CockpitModelRouteUnavailable('no route'), REVISION]
        result = self.run_one()
        self.assertEqual(result['localized']['sections'][0]['body'], '收入为 123 美元。')
        self.assertEqual(self.calls.count(prep.CHECKER_PURPOSE), 1)
        self.assertTrue(any(request.endswith('-output-12000') for request in self.request_ids))

    def test_initial_brain_never_bypasses_an_outstanding_call_with_a_smaller_budget(self):
        self.responses[prep.BRAIN_PURPOSE] = CockpitModelError('this request is already running')
        with self.assertRaisesRegex(ValueError, 'already running'): self.run_one()
        self.assertEqual(self.calls.count(prep.BRAIN_PURPOSE), 1)
        self.assertFalse(any(request.endswith('-output-12000') for request in self.request_ids))

    def test_checker_then_brain_then_semantic_verifier_replays_without_new_calls(self):
        result=self.run_one()
        self.assertEqual(self.calls,['research_localization',prep.CHECKER_PURPOSE,prep.BRAIN_PURPOSE,
                                     'research_localization_verifier'])
        self.assertEqual(self.excluded,[['research_localization',prep.BRAIN_PURPOSE]])
        self.assertEqual(result['localized']['sections'][0]['body'],'收入为 123 美元。')
        self.assertEqual(result['language_review']['numeric_source_hash'],
                         prep.source_content_hash(SOURCE))
        self.assertEqual(len(list(Path(self.temp.name).glob('language-reviews/*.md'))),1)
        self.assertEqual(self.run_one(),result)
        self.assertEqual(len(self.calls),4)

    def test_route_failure_does_not_redraft_or_call_checker(self):
        self.responses['research_localization']=CockpitModelError('model unavailable')
        with self.assertRaises(CockpitModelError):self.run_one()
        self.assertEqual(self.calls,['research_localization'])

    def test_three_invalid_drafts_get_one_cached_brain_repair_before_review(self):
        bad={'sections':[{'index':0,'title':'收入','body':'收入为 124 USD。','gaps':[]}]}
        self.responses['research_localization']=[bad,bad,bad]
        self.responses[prep.BRAIN_PURPOSE]=[CHINESE,REVISION]
        result=self.run_one()
        self.assertEqual(self.calls.count('research_localization'),3)
        self.assertEqual(self.calls.count(prep.BRAIN_PURPOSE),2)
        self.assertEqual(self.calls.count(prep.CHECKER_PURPOSE),1)
        self.assertTrue(any(x.startswith('zh-draft-brain-repair-') for x in self.request_ids))
        self.assertEqual(len(result['draft_failures']),3)
        self.assertEqual(result['draft']['route_decision_ref'],prep.BRAIN_PURPOSE)
        self.assertEqual(self.excluded,[ [prep.BRAIN_PURPOSE,prep.BRAIN_PURPOSE] ])
        calls=len(self.calls);self.assertEqual(self.run_one(),result);self.assertEqual(len(self.calls),calls)

    def test_final_transport_failure_never_uses_brain_draft_repair(self):
        bad={'sections':[{'index':0,'title':'收入','body':'收入为 124 USD。','gaps':[]}]}
        self.responses['research_localization']=[bad,bad,CockpitModelError('broker busy')]
        with self.assertRaisesRegex(CockpitModelError,'broker busy'):self.run_one()
        self.assertEqual(self.calls.count(prep.BRAIN_PURPOSE),0)

    def test_invalid_completed_draft_repair_is_rechecked_without_new_calls(self):
        bad = {'sections': [{'index': 0, 'title': '收入', 'body': '收入为 124 USD。', 'gaps': []}]}
        self.responses['research_localization'] = bad
        self.responses[prep.BRAIN_PURPOSE] = bad
        with self.assertRaises(prep.ResearchLocalizationError):
            self.run_one()
        calls = list(self.calls)
        self.assertEqual(calls.count(prep.BRAIN_PURPOSE), 1)
        with self.assertRaises(prep.ResearchLocalizationError):
            self.run_one()
        self.assertEqual(self.calls, calls)

    def test_worker_two_attempt_budget_still_gets_one_content_repair(self):
        self.args['attempts'] = 2
        bad = {'sections': [{'index': 0, 'title': '收入', 'body': '收入为 124 USD。', 'gaps': []}]}
        self.responses['research_localization'] = [bad, bad]
        self.responses[prep.BRAIN_PURPOSE] = [CHINESE, REVISION]
        self.run_one()
        self.assertEqual(self.calls.count('research_localization'), 2)
        self.assertEqual(self.calls.count(prep.BRAIN_PURPOSE), 2)

    def test_io_errors_are_not_treated_as_repairable_model_content(self):
        self.responses['research_localization'] = OSError('storage unavailable')
        with self.assertRaisesRegex(OSError, 'storage unavailable'): self.run_one()
        self.assertEqual(self.calls, ['research_localization'])

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

    def test_explicit_third_repair_reuses_failed_calls_and_original_checker(self):
        revisions = [copy.deepcopy(REVISION) for _ in range(4)]
        for index, revision in enumerate(revisions):
            revision['sections'][0]['body'] = f'收入为 123 美元。说明{chr(65 + index)}。'
        failed = {**VERDICT, 'verdict': 'fail', 'faithful': False,
                  'findings': ['删除原文没有的额外比较判断。']}
        self.responses[prep.BRAIN_PURPOSE] = revisions
        self.responses['research_localization_verifier'] = [failed, failed, failed, VERDICT]
        self.args['repair_reviewed'] = True
        with self.assertRaisesRegex(ValueError, 'verifier'):
            self.run_one()
        self.assertEqual(self.calls.count(prep.BRAIN_PURPOSE), 3)
        self.args['extra_brain_repair'] = True
        result = self.run_one()
        self.assertEqual(self.calls.count(prep.BRAIN_PURPOSE), 4)
        self.assertEqual(self.calls.count(prep.CHECKER_PURPOSE), 1)
        self.assertEqual(self.calls.count('research_localization'), 1)
        self.assertEqual(self.calls.count('research_localization_verifier'), 4)
        self.assertEqual(len(result['repair_brain_calls']), 3)
        before = len(self.calls)
        self.assertEqual(self.run_one(), result)
        self.assertEqual(len(self.calls), before)

    def test_extra_repair_requires_explicit_reviewed_repair_mode(self):
        self.args['extra_brain_repair'] = True
        with self.assertRaisesRegex(ValueError, 'requires reviewed repair mode'):
            self.run_one()
        self.assertEqual(self.calls, [])

    def test_cached_third_pending_revision_revalidates_without_another_brain(self):
        source = {'kind':'initial_screen','version_ref':'screen:rounded','status':'available',
            'sections':[{'title':'Revenue','body':'Revenue was 1535000000 USD.','gaps':[]}]}
        rounded = {'sections':[{'index':0,'title':'收入','body':'收入为15.35亿美元。','gaps':[]}]}
        restored = {'decisions':[{'suggestion_index':0,'decision':'reject','reason':'恢复原始精度。'}],
            'sections':[{'index':0,'title':'收入','body':'收入为1535000000美元。','gaps':[]}]}
        style = {'overall':'表达清楚。','suggestions':[{'section_index':0,
            'quote':'收入为15.35亿美元。','assessment':'可保留。','suggestion':'收入为15.35亿美元。'}]}
        self.responses.update(research_localization=rounded,
            research_language_check=style,research_language_revision=restored)
        saved = prep.run_chunk((0,0,source),**self.args)[2]
        stage_path = next(Path(self.temp.name).glob('stages/*.json'))
        stage = json.loads(stage_path.read_text())
        call = stage['brain_call']
        pending = prep.run_language_review(
            dict(source,sections=rounded['sections']), checker=lambda _:style,
            brain=lambda _:restored,
            checker_identity={'provider':prep.CHECKER_PROVIDER,'model':prep.CHECKER_MODEL})
        self.assertEqual(pending['status'],'pending_brain_revision')
        stage['repair_brain_calls']=[copy.deepcopy(call) for _ in range(3)]
        stage['review_history']=[{'stage':'brain_repair','attempt':3,
            'brain_call':copy.deepcopy(call),'language_review':pending,
            'prior_brain_call':None,'prior_language_review':None,
            'trigger':{'stage':'brain_validation'}}]
        stage_path.write_text(json.dumps(stage))
        for path in Path(self.temp.name).glob('chunks/*.json'):path.unlink()
        for path in Path(self.temp.name).glob('semantic-stages/*.json'):path.unlink()
        before_brain=self.calls.count(prep.BRAIN_PURPOSE)
        before_checker=self.calls.count(prep.CHECKER_PURPOSE)
        before_verifier=self.calls.count('research_localization_verifier')
        args={**self.args,'repair_reviewed':True,'extra_brain_repair':True}
        recovered=prep.run_chunk((0,0,source),**args)[2]
        self.assertEqual(recovered['status'],'passed')
        self.assertEqual(self.calls.count(prep.BRAIN_PURPOSE),before_brain)
        self.assertEqual(self.calls.count(prep.CHECKER_PURPOSE),before_checker)
        self.assertEqual(self.calls.count('research_localization_verifier'),before_verifier+1)
        rows=json.loads(stage_path.read_text())['review_history']
        self.assertEqual([r['stage'] for r in rows].count('brain_revalidation'),1)
        before=len(self.calls)
        self.assertEqual(prep.run_chunk((0,0,source),**args)[2],recovered)
        self.assertEqual(len(self.calls),before)
        self.assertEqual([r['stage'] for r in json.loads(stage_path.read_text())[
            'review_history']].count('brain_revalidation'),1)

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

    def test_reviewed_repair_does_not_redraft_for_semantic_transport_failure(self):
        self.responses['research_localization_verifier']=[
            CockpitModelError('temporary verifier outage'),VERDICT]
        self.args['repair_reviewed']=True
        with self.assertRaisesRegex(CockpitModelError,'temporary verifier outage'):
            self.run_one()
        result=self.run_one()
        self.assertEqual(result['status'],'passed')
        self.assertEqual(self.calls.count(prep.CHECKER_PURPOSE),1)
        self.assertEqual(self.calls.count(prep.BRAIN_PURPOSE),1)
        self.assertEqual(self.calls.count('research_localization_verifier'),2)
