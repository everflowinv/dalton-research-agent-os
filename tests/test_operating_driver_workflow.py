import copy
import unittest
from unittest.mock import patch
from dalton_core.store import content_hash
from dalton_core.directed_numeric_output import candidate_ref,quotes
from dalton_core.directed_numeric_research import normalize_targets
from dalton_core.operating_driver_units import CONTRACT_REF
from dalton_core.operating_driver_source_plan import TASK_REF,SCHEMA_VERSION
from dalton_core.mission_document_research_admission import admit_directed_inquiry
from dalton_core.research_question_backlog import ResearchQuestionBacklog
from dalton_core.operating_driver_context import project_result,validate_context,metric_history,CONTEXT_REF
from tests import test_directed_numeric_workflow as numeric_fixture
from tests.test_mission_document_research import RouteBoundCountingFakeAdapter


class OperatingWorkflowTests(unittest.TestCase):
    def fixture(self):
        # Genuine normal source/independent model verifier/promotion lifecycle.
        # This fixture isolates the new numeric contract. The model-source
        # origin's separate root replay is tested by the owner handoff suite.
        support=numeric_fixture.NumericWorkflowTests('test_three_required_figures_formal_claims_and_zero_call_replay')
        self.addCleanup(support.doCleanups)
        f,authority,old,_,_,_=support.fixture(extra_text='\n2026-01-01/2026-03-31 Units sold (tons) | 40\n2026-01-01/2026-03-31 Average selling price (USD per ton) | 12')
        registration=old['request']['registration']
        targets=normalize_targets([{'target_ref':'operating-target:'+ref,'metric_ref':'metric:operating-'+ref,
            'metric_definition':{'label':label,'meaning':'Issuer reported '+label+' without physical unit conversion.'},
            'period':'2026-01-01/2026-03-31','unit':unit,'currency':currency,'basis':'management-reported'}
            for ref,label,unit,currency in [('quantity','Units sold','tons',None),('price','Average selling price','currency_per_ton','USD')]],contract_ref=CONTRACT_REF)
        inquiry={'rank':0,'company_ref':old['company_ref'],'question':'What were units sold and average selling price during 2026 Q1?',
            'wants':'Reported physical quantities, not inferred revenue.','because':'Read exact reported original.',
            'directed_document':{'strategy_version':CONTRACT_REF,'document_ref':registration['document_ref'],
                'document_version_hash':registration['content_hash'],'query_terms':['Units sold'],
                'query_rationale':'Read the exact issuer operating quantities.','numeric_targets':targets}}
        plan={'schema_version':SCHEMA_VERSION,'task_ref':TASK_REF,'mission_version_ref':f.mission['id'],
              'state_hash':'7'*64,'assessment':'Owner origin isolated numeric contract fixture.',
              'directives':[],'inquiries':[inquiry],'sufficiency':[],'model_source_origin':{'contract_ref':TASK_REF}}
        plan['content_hash']=content_hash(plan)
        stored=authority._missions.record_research_plan(plan,decided_by=f.mission['autonomy']['automation_principal'])
        exact={**plan,'plan_id':stored['plan_id']}
        authority.model_source_plan_resolver=lambda supplied,mission: plan
        admitted=admit_directed_inquiry(authority=authority,registrations={registration['id']:registration},
            backlog=ResearchQuestionBacklog(f.store),mission=f.mission,plan=exact,inquiry=inquiry)
        admission=authority.resolve_for_execution(admitted['admission_ref'])
        proof=authority.registry.search(admission['request']);qid=next(iter(quotes(proof)))
        rows=[]
        for target,value in zip(targets,['40','12']):
            rows.append({'target_ref':target['target_ref'],'status':'supported','figure':{
                'quote_id':qid,'metric_ref':target['metric_ref'],'subject_as_named':'Synthetic issuer',
                'as_reported_label':target['metric_definition']['label'],'value':value,
                **{k:target[k] for k in ('unit','currency','period','basis')},'scale':None},'reason':None})
        draft={'schema_version':CONTRACT_REF,'targets':rows}
        verdict={'schema_version':'0.1','candidate_ref':candidate_ref(draft),'verdict':'pass',
            'targets':[{'target_ref':t['target_ref'],'verdict':'accept','reason':None} for t in targets]}
        executor,producer,verifier=support._executor(f,authority,draft_adapter=RouteBoundCountingFakeAdapter(draft),
            verifier_adapter=RouteBoundCountingFakeAdapter(verdict))
        return support,f,admission,executor,producer,verifier

    def test_physical_values_become_canonical_claims_and_replay_not_counts(self):
        support,f,admission,executor,producer,verifier=self.fixture()
        result=support.drive(executor,admission)
        self.assertEqual(result['research_status'],'numeric_targets_completed')
        from dalton_core.mission_document_research_executor import effective_mission_document_work_orders
        works=effective_mission_document_work_orders(executor.authority,executor.scheduler,admission['id'],
            draft_worker=executor.draft_worker,verifier_worker=executor.verifier_worker)
        rows=project_result(executor=executor,admission=admission,works=works)
        body={'contract_ref':CONTEXT_REF,'company_ref':admission['company_ref'],'records':rows}
        context={**body,'content_hash':content_hash(body)}
        validate_context(context,company_ref=admission['company_ref'])
        history=metric_history(context,company_ref=admission['company_ref'],metric_ref='metric:operating-quantity')
        self.assertEqual(history['unit'],'tons');self.assertEqual(history['history'][0]['value'],'40')
        price=metric_history(context,company_ref=admission['company_ref'],metric_ref='metric:operating-price')
        self.assertEqual(price['unit'],'usd_per_ton')
        self.assertEqual(result,executor.run_once(admission['id']))
        self.assertEqual((producer.calls,verifier.calls),(1,1))

if __name__=='__main__':unittest.main()
