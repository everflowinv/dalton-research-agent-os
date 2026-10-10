from __future__ import annotations
import copy,json,sqlite3,tempfile,unittest
from pathlib import Path
from datetime import datetime,timezone
from dalton_core.store import DaltonStore,content_hash,canonical_json
from dalton_core.industry_framework import IndustryFrameworkAuthority
from dalton_core.research_followup import ResearchFollowupAuthority,requests
from dalton_core.research_followup_automation import FollowupAutomationAuthority,groups,latest
from dalton_core.research_followup_product_repair import context,pending_units,prior_body,verify_result,complete,existing_candidates
from dalton_core.industry_framework_draft import material_rows,parse_unit_output,build_unit_prompt,build_verifier_prompt
from dalton_core.research_product_consumption import scope,record as consume
from dalton_core.model_router import _profile_wire
from tests import test_industry_framework as fixture
from tests.test_model_router import profile


class FrameworkFormalRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.store=DaltonStore(str(self.root/'core.sqlite'));self.addCleanup(self.store.close);self.c=self.store.connection
        self.products=IndustryFrameworkAuthority(self.store);self.followups=ResearchFollowupAuthority(self.store);self.auto=FollowupAutomationAuthority(self.store)
        body=fixture.record(comparison=fixture.simple_comparison())
        body['sections'][0]['slots'][0]['sentences'][0]['text']='收入为5。'
        body['sections'][0]['sources'][0]['text']='收入为5。'
        if getattr(self,'additional_unit',False):
            body['sections'][1]['title']='t2'
            body['sections'][1]['slots'][0]['sentences'][0]['text']='收入为7。'
            body['sections'][1]['sources'][0]['text']='收入为7。'
        self.original_body=copy.deepcopy(body)
        self.old=self.products.publish(body)
        self.followups.capture_existing([self.old['id']]);self.items=context(self.c,'industry_framework',fixture.INDUSTRY,self.old)
        self.assertEqual(pending_units(self.c,self.items),['causal_chain:0','causal_chain:1'] if getattr(self,'additional_unit',False) else ['causal_chain:0'])
        self.unit='causal_chain:0';self.mission={'id':'mission:repair','content_hash':'a'*64}
        self.material=material_rows([{'kind':'claim','ref':'claim-version:new','text':'2026年收入为5。','period':'2026-09-30'}])
        self.inp={'unit':self.unit,'structure':[{'slot_id':'state','prompt':'State'},{'slot_id':'divergence','prompt':'Difference'}],
                  'material':self.material,'industry':{'industry_ref':fixture.INDUSTRY},'mission':self.mission,
                  'link':fixture.CHAIN[0],'title':'t','link_index':0,'prior_body':prior_body('Held prior prose.',self.items),'comparison_table':''}
        self.text=json.dumps({'slots':[{'slot_id':'state','sentences':[{'text':'2026年收入为5。','refs':['C1']}]},
                           {'slot_id':'divergence','unknown':'材料不足'}],'gaps':[]},ensure_ascii=False)
        self.block=parse_unit_output(self.text,**{k:self.inp[k] for k in ('unit','structure','material','link','title','link_index')})
        new=copy.deepcopy(body);new['sections'][0]=self.block;new['evidence_refs']=self.block['sources']
        self.new=self.products.publish(new);self.assertEqual(self.new['status'],'fresh')
        self.scheduler=sqlite3.connect(self.root/'scheduler.sqlite');self.router=sqlite3.connect(self.root/'model-router.sqlite')
        self.addCleanup(self.scheduler.close);self.addCleanup(self.router.close)
        self.scheduler.executescript('CREATE TABLE scheduler_work_orders(work_order_id TEXT,work_order_json TEXT,work_order_hash TEXT); CREATE TABLE scheduler_result_envelopes(result_envelope_id TEXT,work_order_id TEXT,attempt_number INTEGER,result_envelope_json TEXT,result_envelope_hash TEXT,outcome TEXT,content_hash TEXT,created_at TEXT);')
        self.router.executescript('CREATE TABLE model_route_decisions(decision_id TEXT,work_order_id TEXT,work_order_hash TEXT,outcome TEXT,decision_json TEXT,decision_hash TEXT); CREATE TABLE model_endpoint_profile_versions(profile_version_ref TEXT,profile_hash TEXT,profile_json TEXT);')
        self.producer=self.call('producer','industry_framework',build_unit_prompt(**{k:self.inp[k] for k in ('unit','structure','material','industry','link','title','prior_body','comparison_table')}),self.text,[],family='family-alpha')
        self.verifier=self.call('verifier','industry_framework_verifier',build_verifier_prompt({self.unit:self.block},industry=self.inp['industry']),'{"verdict":"pass","findings":[]}',[self.producer],family='family-beta')
        stamp=datetime.now(timezone.utc).isoformat();snapshot={'captured_at':stamp,'input_hash':content_hash(self.inp),'exact_input_hash':content_hash(self.inp),'read_refs':[], 'read_documents':[],'arrivals':[]}
        self.receipt=consume(self.root,scope('industry_framework',fixture.INDUSTRY,self.unit,['company:synthetic']),snapshot,result='published',producer_refs=[self.producer],verifier_ref=self.verifier,independent=True,verified=True)
        self.evidence={'kind':'verified_product_repair','product':'industry_framework','record_ref':self.new['id'],'record_hash':self.new['content_hash'],
                       'prior_ref':self.old['id'],'prior_hash':self.old['content_hash'],'context':self.items,
                       'units':{self.unit:{'scope':self.receipt['scope'],'receipt_hash':self.receipt['content_hash'],'input':self.inp}}}

    def call(self,name,purpose,prompt,text,producers,*,family):
        endpoint=_profile_wire(profile(name,family=family));ref='route:'+name
        self.router.execute('INSERT INTO model_endpoint_profile_versions VALUES(?,?,?)',(endpoint['profile_version_ref'],endpoint['content_hash'],canonical_json(endpoint)))
        work={'id':'work:'+name,'question':prompt,'metadata':{'purpose':purpose,'mission_version_ref':self.mission['id'],'mission_version_hash':self.mission['content_hash'],'producer_route_decision_refs':producers}}
        workhash=content_hash(work);decision={'id':ref,'selected_profile_version_ref':endpoint['profile_version_ref'],'selected_profile_hash':endpoint['content_hash']};decision['content_hash']=content_hash(decision)
        self.router.execute('INSERT INTO model_route_decisions VALUES(?,?,?,?,?,?)',(ref,work['id'],workhash,'selected',canonical_json(decision),decision['content_hash']))
        self.scheduler.execute('INSERT INTO scheduler_work_orders VALUES(?,?,?)',(work['id'],canonical_json(work),workhash))
        result={'id':'result:'+name,'work_order_ref':work['id'],'status':'succeeded','outputs':{'text':text},'metadata':{'route_decision_ref':ref}}
        receipt={'result_envelope_id':result['id'],'work_order_id':work['id'],'attempt_number':1,'result_envelope_hash':content_hash(result),'outcome':'succeeded','created_at':'2026-10-10T12:00:00+00:00'}
        self.scheduler.execute('INSERT INTO scheduler_result_envelopes VALUES(?,?,?,?,?,?,?,?)',(result['id'],work['id'],1,canonical_json(result),content_hash(result),'succeeded',content_hash(receipt),receipt['created_at']))
        self.scheduler.commit();self.router.commit();return ref

    def test_source_bound_normal_formal_producer_verifier_and_product_close_without_changing_history(self):
        old=list(self.c.execute('SELECT record_json,content_hash FROM research_followup_requests'))
        group=self.items[0]['group'];self.assertEqual(verify_result(self.c,self.evidence,group),self.evidence)
        out=complete(self.auto,product='industry_framework',published=self.new,prior=self.old,items=self.items,inputs={self.unit:self.inp},receipts={self.unit:self.receipt})
        self.assertEqual(len(out),1);self.assertEqual(latest(self.c,group['group_ref'])['state'],'completed')
        self.assertEqual(old,list(self.c.execute('SELECT record_json,content_hash FROM research_followup_requests')))
        count=self.c.execute('SELECT count(*) FROM research_followup_automation_versions').fetchone()[0]
        complete(self.auto,product='industry_framework',published=self.new,prior=self.old,items=self.items,inputs={self.unit:self.inp},receipts={self.unit:self.receipt})
        self.assertEqual(self.c.execute('SELECT count(*) FROM research_followup_automation_versions').fetchone()[0],count)
        self.assertEqual(pending_units(self.c,self.items),[])
        self.assertEqual(self.products.framework(self.old['id'])['content_hash'],self.old['content_hash'])

    def test_unrelated_new_version_missing_receipt_tampered_input_and_unbound_source_cannot_close(self):
        for update in [{'record_hash':'0'*64},{'prior_hash':'0'*64},{'units':{}},
                       {'units':{self.unit:{**self.evidence['units'][self.unit],'receipt_hash':'0'*64}}},
                       {'units':{self.unit:{**self.evidence['units'][self.unit],'input':{**self.inp,'prior_body':'A fake review'}}}}]:
            with self.subTest(update=update):
                with self.assertRaises(ValueError):verify_result(self.c,{**self.evidence,**update},self.items[0]['group'])
        self.assertIsNone(latest(self.c,self.items[0]['group']['group_ref']))

    def test_no_fake_family_independence_even_with_claimed_consumption_receipt(self):
        row=self.router.execute('SELECT decision_json FROM model_route_decisions WHERE decision_id=?',(self.verifier,)).fetchone()
        d=json.loads(row[0]);p=_profile_wire(profile('producer',family='family-alpha'));d.update({'selected_profile_version_ref':p['profile_version_ref'],'selected_profile_hash':p['content_hash']});d['content_hash']=content_hash({k:v for k,v in d.items() if k!='content_hash'})
        self.router.execute('UPDATE model_route_decisions SET decision_json=?,decision_hash=? WHERE decision_id=?',(canonical_json(d),d['content_hash'],self.verifier));self.router.commit()
        with self.assertRaisesRegex(ValueError,'independent'):verify_result(self.c,self.evidence,self.items[0]['group'])

    def test_exact_group_human_decision_precedes_system_completion(self):
        request=requests(self.c)[0];self.followups.decide(request['id'],action='declined',actor_ref='human:owner',reason='Do not perform this repair.')
        with self.assertRaisesRegex(ValueError,'human already decided'):
            self.auto.observe(self.items[0]['group'],state='completed',reason='Formal repair',evidence=self.evidence)
        self.assertEqual(complete(self.auto,product='industry_framework',published=self.new,prior=self.old,items=self.items,inputs={self.unit:self.inp},receipts={self.unit:self.receipt}),[])

    def test_verified_no_new_evidence_waits_without_repeating_the_same_paid_input(self):
        from dalton_core.research_followup_product_repair import unread_repair_units
        self.assertEqual(unread_repair_units(self.root,'industry_framework',fixture.INDUSTRY,['company:synthetic'],[self.unit],{self.unit:self.inp}),[])
        new_input={**self.inp,'comparison_table':'A genuinely new filed comparison.'}
        self.assertEqual(unread_repair_units(self.root,'industry_framework',fixture.INDUSTRY,['company:synthetic'],[self.unit],{self.unit:new_input}),[self.unit])
        snapshot={'captured_at':datetime.now(timezone.utc).isoformat(),'input_hash':content_hash(new_input),'exact_input_hash':content_hash(new_input),'read_refs':[], 'read_documents':[],'arrivals':[]}
        consume(self.root,scope('industry_framework',fixture.INDUSTRY,self.unit,['company:synthetic']),snapshot,result='verified_no_new_evidence',producer_refs=[self.producer],verifier_ref=self.verifier,independent=True,verified=True)
        self.assertEqual(unread_repair_units(self.root,'industry_framework',fixture.INDUSTRY,['company:synthetic'],[self.unit],{self.unit:new_input}),[])
        self.assertEqual(pending_units(self.c,self.items),[self.unit])

    def test_bounded_prompt_contains_all_identity_proof_before_long_prior_prose(self):
        bounded=prior_body('x'*8000,self.items)
        self.assertLessEqual(len(bounded),2000)
        prompt=build_unit_prompt(**{**{k:self.inp[k] for k in ('unit','structure','material','industry','link','title','comparison_table')},'prior_body':bounded})
        self.assertIn(self.items[0]['group']['content_hash'],prompt)
        self.assertIn(self.items[0]['instructions'][0]['hash'],prompt)


class ExistingDossierCandidateTests(unittest.TestCase):
    def setUp(self):
        from tests.test_company_model_forecast import LaneStateTests
        from dalton_core.company_dossier import CompanyDossierAuthority
        from tests import test_company_dossier as dossier
        self.f=LaneStateTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.c=self.f.store.connection;self.authority=CompanyDossierAuthority(self.f.store)
        section=dossier.drafted('business_model','claim-version:legacy-repair',text='收入为5。',source_text='收入为5。')
        section['sources'][0]['period']=None
        self.old=self.authority.publish(dossier.body(drafted_sections={'business_model':section}))
        self.followup=ResearchFollowupAuthority(self.f.store);self.followup.capture_existing([self.old['id']])

    def test_existing_formal_file_enters_repair_without_fabricating_screen_or_current_grant(self):
        from types import SimpleNamespace
        from dalton_core.company_dossier_cli import screened_companies
        before=list(self.c.execute('SELECT * FROM coverage_mission_stage_records'))
        fake_screen_reader=SimpleNamespace(connection=self.c,stage_state_by_company=lambda ref:{})
        self.assertIn(self.old['company_ref'],existing_candidates(self.c,self.f.mission))
        self.assertEqual(screened_companies(fake_screen_reader,self.f.mission),[self.old['company_ref']])
        self.assertEqual(before,list(self.c.execute('SELECT * FROM coverage_mission_stage_records')))
        self.assertEqual(self.f.missions.mission(self.f.mission['id'])['budget'],self.f.mission['budget'])
        self.assertEqual(existing_candidates(self.c,{**self.f.mission,'universe':[]}),[])
        self.assertEqual(existing_candidates(self.c,{**self.f.mission,'id':'mission-version:foreign'}),[])

    def test_same_source_no_new_task_repeat_context_and_human_decision_removes_candidate(self):
        first=context(self.c,'dossier',self.old['company_ref'],self.old,mission=self.f.mission)
        self.assertEqual(first,context(self.c,'dossier',self.old['company_ref'],self.old,mission=self.f.mission))
        self.assertEqual(pending_units(self.c,first),['business_model'])
        original=list(self.c.execute('SELECT record_json,content_hash FROM research_followup_requests'))
        existing_candidates(self.c,self.f.mission)
        self.assertEqual(original,list(self.c.execute('SELECT record_json,content_hash FROM research_followup_requests')))
        request=requests(self.c)[0]
        self.followup.decide(request['id'],action='declined',actor_ref='human:owner',reason='Owner explicitly declines the repair.')
        self.assertEqual(existing_candidates(self.c,self.f.mission),[])



class SplitUnitRepairTests(unittest.TestCase):
    def test_original_unit_limit_splits_work_without_repeating_verified_unit_or_false_completion(self):
        f=FrameworkFormalRepairTests('test_source_bound_normal_formal_producer_verifier_and_product_close_without_changing_history')
        f.additional_unit=True;f.setUp();self.addCleanup(f.doCleanups)
        original=list(f.c.execute('SELECT record_json,content_hash FROM research_followup_requests'))
        first=complete(f.auto,product='industry_framework',published=f.new,prior=f.old,items=f.items,inputs={f.unit:f.inp},receipts={f.unit:f.receipt})
        self.assertEqual(first[0]['state'],'waiting_evidence')
        self.assertEqual(pending_units(f.c,f.items),['causal_chain:1'])
        unit='causal_chain:1';inp={**f.inp,'unit':unit,'title':'t2','link':fixture.CHAIN[1],'link_index':1,
            'material':material_rows([{'kind':'claim','ref':'claim-version:new2','text':'2026年收入为7。','period':'2026-09-30'}])}
        text=json.dumps({'slots':[{'slot_id':'state','sentences':[{'text':'2026年收入为7。','refs':['C1']}]},{'slot_id':'divergence','unknown':'材料不足'}],'gaps':[]},ensure_ascii=False)
        block=parse_unit_output(text,**{k:inp[k] for k in ('unit','structure','material','link','title','link_index')})
        body=copy.deepcopy(f.original_body);body['sections'][0]=f.block;body['sections'][1]=block;body['evidence_refs']=block['sources']
        final=f.products.publish(body)
        producer=f.call('producer2','industry_framework',build_unit_prompt(**{k:inp[k] for k in ('unit','structure','material','industry','link','title','prior_body','comparison_table')}),text,[],family='family-alpha')
        verifier=f.call('verifier2','industry_framework_verifier',build_verifier_prompt({unit:block},industry=inp['industry']),'{"verdict":"pass","findings":[]}',[producer],family='family-beta')
        snapshot={'captured_at':datetime.now(timezone.utc).isoformat(),'input_hash':content_hash(inp),'exact_input_hash':content_hash(inp),'read_refs':[], 'read_documents':[],'arrivals':[]}
        receipt=consume(f.root,scope('industry_framework',fixture.INDUSTRY,unit,['company:synthetic']),snapshot,result='published',producer_refs=[producer],verifier_ref=verifier,independent=True,verified=True)
        result=complete(f.auto,product='industry_framework',published=final,prior=f.new,items=f.items,inputs={unit:inp},receipts={unit:receipt})
        self.assertEqual(result[0]['state'],'completed')
        self.assertEqual(len(result[0]['evidence']['unit_proofs']),2)
        self.assertEqual(pending_units(f.c,f.items),[])
        self.assertEqual(original,list(f.c.execute('SELECT record_json,content_hash FROM research_followup_requests')))
        self.assertEqual(verify_result(f.c,result[0]['evidence'],f.items[0]['group']),result[0]['evidence'])
        # Tampering with the latest supposedly carried unit defeats the exact
        # ancestry proof; neither an unrelated version nor old work closes it.
        bad=copy.deepcopy(result[0]['evidence']);bad['record_hash']='0'*64
        with self.assertRaises(ValueError):verify_result(f.c,bad,f.items[0]['group'])
        with self.assertRaisesRegex(ValueError,'group hash'):
            verify_result(f.c,{**result[0]['evidence'],'group_hash':'0'*64},f.items[0]['group'])


class TypedTopLevelParagraphBindingTests(unittest.TestCase):
    def test_driver_blocks_bind_by_typed_key_and_exact_paragraph_bytes(self):
        from dalton_core.historical_research_context import reading_projection
        from dalton_core.research_followup_product_repair import affected_units
        source={'id':'industry-framework-version:synthetic:1','content_hash':'a'*64,'sections':[],
            'long_term_drivers':{'slots':[{'slot_id':'driver:l','sentences':[{'text':'收入为5。','refs':[]}]}]},
            'short_term_drivers':{'slots':[{'slot_id':'driver:s','sentences':[{'text':'成本为7。','refs':[]}]}]}}
        projection=reading_projection(source)
        request={'scope':{'reading_projection':projection}}
        self.assertEqual(affected_units(source,request,'industry_framework'),['long_term_drivers','short_term_drivers'])
        for key,value in [('text_hash','0'*64),('ordinal',1),('slot_id','unrelated'),('unit','unrelated')]:
            changed=copy.deepcopy(request);changed['scope']['reading_projection']['paragraphs'][0][key]=value
            with self.subTest(key=key),self.assertRaisesRegex(ValueError,'cannot bind an exact'):
                affected_units(source,changed,'industry_framework')
