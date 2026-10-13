import copy
import hashlib
import json
import unittest
from unittest.mock import patch
from dalton_core.store import content_hash,canonical_json
from dalton_core.planner_note_options import option_ref,prompt_state,expand_formal_selection
from dalton_core.document_research_strategy import DocumentResearchStrategyError
from dalton_core.mission_document_research import _exact_plan,MissionDocumentResearchError
from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.contracts import WorkOrder,ResultEnvelope
from dalton_core.research_task import inquiry_content_hash,inquiry_ref_for
from tests.test_document_research_strategy import evidence_target,strategy
from tests import test_financial_note_dual_source as note_support
from tests.test_mission_annual_research import NOW
from tests import test_mission_research_task_lane as lane_support
from dalton_core.mission_research_task_lane import ResearchTaskCoordinator


def recorded_alias_plan(f, original):
    reg=original['request']['registration'];inquiry=copy.deepcopy(original['planner_inquiry']);target=inquiry['directed_document']['evidence_target']
    doc={'document_ref':reg['document_ref'],'document_version_hash':reg['content_hash'],'evidence_targets':[target]}
    full={'companies':[{'company_ref':original['company_ref'],'readable_documents':[doc]}]};full['content_hash']=content_hash(full)
    frozen,_=prompt_state(full)
    raw_i=copy.deepcopy({k:v for k,v in inquiry.items()if k not in('rank','repair_target_hash')})
    raw_i['directed_document'].pop('evidence_target');raw_i['directed_document']['evidence_target_ref']=option_ref(original['company_ref'],doc,target)
    raw={'schema_version':'0.1','assessment':'Read the original chosen financial note.','directives':[],'inquiries':[raw_i],'sufficiency':[]}
    text=json.dumps(raw);digest=full['content_hash']
    plan={'schema_version':'0.1','task_ref':'task:research-plan-directives:0.1','created_at':f.mission['created_at'],'state_hash':digest,'mission_version_ref':f.mission['id'],'assessment':raw['assessment'],'directives':[],'inquiries':[inquiry],'sufficiency':[]};plan['content_hash']=content_hash(plan)
    work=WorkOrder.from_dict({'schema_version':'0.1','id':'work:cockpit-plan-'+digest[:32],'created_at':NOW.isoformat(),'updated_at':NOW.isoformat(),'question':'Frozen original\nRESEARCH_STATE='+canonical_json(frozen),'requested_capabilities':['research'],'runtime_profile_ref':'runtime-profile:dalton-model-broker:0.1','budget':{'max_input_tokens':1000,'max_output_tokens':1000,'max_total_tokens':2000,'max_cost_usd':1.0,'max_seconds':30},'idempotency_key':'alias-plan:'+digest,'declared_side_effects':[],'status':'ready','input_refs':[],'metadata':{'control_plane':'cockpit','purpose':'plan','request_id':digest[:32],'mission_version_ref':f.mission['id']}})
    scheduler=f.harness.scheduler();scheduler.enqueue(work.to_dict());claim=scheduler.claim('worker:note-fixture',work_order_id=work.id)
    route=f.router.route(work,attempt_number=claim['attempt']['attempt_number'],capability='research',policy_version_ref=f.draft_policy['policy_version_ref'],credential_slot_refs=[f.draft_profile['credential_slot_ref']],required_modalities=['text'],required_context_tokens=1000,estimated_input_tokens=100,estimated_output_tokens=100,idempotency_key='note-route:'+digest,tier='brain',purpose='registered_annual_report_draft')['decision']
    envelope=ResultEnvelope(schema_version='0.1',id='result-envelope:note-planner:'+digest[:16],created_at=NOW.isoformat(),work_order_ref=work.id,invocation_ref='model-invocation:note-fixture:'+digest[:16],status='succeeded',outputs={'text':text,'content_hash':hashlib.sha256(text.encode()).hexdigest()},actual_side_effects=(),usage_refs=(),artifact_refs=(),error=None,metadata={'route_decision_ref':route['id'],'profile_version_ref':route['selected_profile_version_ref']}).to_dict()
    scheduler.complete(work.id,claim['attempt']['attempt_number'],'worker:note-fixture',claim['lease_token'],envelope,idempotency_key='note-complete:'+digest,result_envelope_hash=content_hash(envelope))
    saved=CoverageMissionAuthority(f.store).record_research_plan(plan,decided_by=f.mission['autonomy']['automation_principal'],work_order_ref=work.id)
    return saved,inquiry


class FormalNoteHandoffTests(unittest.TestCase):
    def simple(self):
        company='company:synthetic:a';target=evidence_target();stored=strategy(evidence_target=target)
        doc={'document_ref':stored['document_ref'],'document_version_hash':stored['document_version_hash'],'evidence_targets':[target]}
        full={'companies':[{'company_ref':company,'readable_documents':[doc]}]};full['content_hash']=content_hash(full);prompt,_=prompt_state(full)
        raw=strategy(evidence_target_ref=option_ref(company,doc,target))
        return company,stored,prompt,raw

    def test_exact_legacy_target_expands_without_changing_raw_or_saved_proof(self):
        company,stored,prompt,raw=self.simple();before=copy.deepcopy((stored,prompt,raw))
        self.assertEqual(expand_formal_selection(raw,company_ref=company,stored_strategy=stored,prompt_state=prompt,state_hash=prompt['content_hash']),stored)
        self.assertEqual((stored,prompt,raw),before)

    def test_wrong_company_source_period_lineage_option_and_state_refuse(self):
        for change in('company','source','period','lineage','option','state','missing','ambiguous','both'):
            company,stored,prompt,raw=self.simple();expected=prompt['content_hash']
            if change=='company':company='company:synthetic:foreign'
            elif change=='source':raw['document_version_hash']='f'*64
            elif change=='period':stored['evidence_target']['periods'][0]['period_start']='2024-08-31'
            elif change=='lineage':stored['evidence_target']['statement_filing_hash']='e'*64
            elif change=='option':raw['evidence_target_ref']='planner-financial-note-option:0.1:'+'0'*64
            elif change=='state':expected='a'*64
            elif change=='missing':prompt['companies'][0]['readable_documents'][0]['evidence_target_options']=[]
            elif change=='ambiguous':prompt['companies'][0]['readable_documents'][0]['evidence_target_options']*=2
            else:raw['evidence_target']=stored['evidence_target']
            with self.subTest(change=change),self.assertRaises(DocumentResearchStrategyError):expand_formal_selection(raw,company_ref=company,stored_strategy=stored,prompt_state=prompt,state_hash=expected)

    def test_real_formal_route_and_dual_source_owner_derivation_two_companies(self):
        for company in ('synthetic:issuer:A','synthetic:issuer:B'):
            h=note_support.DualSourceNoteTests();self.addCleanup(h.doCleanups);f,executor,original,target=h.fixture(company)
            saved,inquiry=recorded_alias_plan(f,original);authority=executor.authority
            before=f.store.connection.total_changes
            derived=authority._derive(plan_ref=saved['plan_id'],inquiry_ref=inquiry_ref_for(inquiry_content_hash(inquiry)),question_version_ref=original['question_version_ref'],document_authority_ref=original['document_authority_ref'])
            self.assertEqual(derived['planner_inquiry']['directed_document']['evidence_target'],target)
            self.assertEqual(derived['company_ref'],company);self.assertEqual(f.store.connection.total_changes,before)
            exact,row=_exact_plan(f.store.connection,saved['plan_id'])
            modified=copy.deepcopy(inquiry);modified['wants']='A different requested fact'
            with self.assertRaisesRegex(MissionDocumentResearchError,'absent or ambiguous'):authority._planner_origin(exact,row,modified,f.mission)

    def test_unpaid_admission_pass_wakes_once_then_restart_holds_without_new_authority(self):
        h=lane_support.LaneTests('runTest');h.setUp();self.addCleanup(h.doCleanups)
        h.record_plan([lane_support.inquiry(question='Read the chosen EPS original.')])
        current=h.coordinator._signature
        def old_signature(plan):
            held=current(plan);held.pop('formal_note_selection');return held
        with patch.object(h.coordinator,'_signature',side_effect=old_signature):
            first=h.coordinator.dispatch_once()
            h.launcher.settle(first['ticket_ref'],status='succeeded',summary={'status':'succeeded','admitted':0,'refused':[]})
            self.assertEqual(h.coordinator.dispatch_once()['status'],'held')
        second=h.coordinator.dispatch_once();self.assertEqual(second['status'],'launched')
        h.launcher.settle(second['ticket_ref'],status='succeeded',summary={'status':'succeeded','admitted':0,'refused':[]})
        self.assertEqual(h.coordinator.dispatch_once()['status'],'held')
        restarted=ResearchTaskCoordinator(store=h.store,launcher=h.launcher,clock=lambda:h.now)
        self.assertEqual(restarted.dispatch_once()['status'],'held')
        self.assertEqual(len(h.launcher.started),2)
