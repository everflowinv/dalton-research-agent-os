"""Bounded priority service preserves the ordinary sweep and source authority."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import json
import sqlite3
import tempfile
import time
import threading
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

from dalton_core import material_acquisition as ma, material_requests as mr
from dalton_core import material_current_gap_priority as priority
from dalton_core.forecast_material_gaps import gap_plans, open_gap_requests
from dalton_core.store import canonical_json, content_hash
from tests import test_material_acquisition as fixtures


def proofs(requests):
    return {r['id']: {'content_hash': content_hash(r['id']), 'request_hash':r['content_hash']} for r in requests}


class PriorityServiceTests(unittest.TestCase):
    def setUp(self):
        self.fx=fixtures.Fixture();self.fx.setUp();self.addCleanup(self.fx.doCleanups);self.state=self.fx.state
        self.normal=[self.fx.request(need={'kind':'answer','question':f'Ordinary {n}'},lane='research_planner') for n in range(10)]
        self.urgent=[self.fx.request(need={'kind':'answer','question':f'Blocked deliverable {n}'},lane='initial_screen')for n in range(3)]

    def ticks(self,count,serve,**options):
        with patch.object(priority,'priority_gaps',return_value=(proofs(self.urgent),{'qualified':3})),patch.object(ma,'serve',side_effect=serve),patch.object(priority,'still_current',return_value=True):
            return [ma.tick(self.state,now=fixtures.NOW,convert=False,**options)for _ in range(count)]

    def test_priority_cohort_rotates_and_ordinary_keeps_three_slots(self):
        seen=[]
        def serve(state,ref,**kw):seen.append(ref);return {'request_id':ref,'outcome':'waiting'}
        summaries=self.ticks(4,serve)
        for summary in summaries:
            self.assertEqual(summary['served'],4)
            self.assertEqual(sum(r['selection_class']=='current_product_gap'for r in summary['results']),1)
            self.assertEqual(sum(r['selection_class']=='ordinary_round_robin'for r in summary['results']),3)
        chosen=[next(r['request_id']for r in s['results']if r['selection_class']=='current_product_gap')for s in summaries]
        ordered=sorted(self.urgent,key=lambda x:ma.priority_key(x,{}))
        self.assertEqual(chosen,[r['id']for r in ordered]+[ordered[0]['id']])
        self.assertTrue({r['id']for r in self.normal}.issubset(seen))
        self.assertEqual(mr.request(self.state,self.urgent[0]['id'])['attempt_count'],0)

    def test_single_slot_alternates_actual_opportunities(self):
        seen=[]
        summaries=self.ticks(6,lambda s,r,**kw:seen.append(r)or{'request_id':r,'outcome':'waiting'},max_requests=1)
        self.assertEqual([s['results'][0]['selection_class']for s in summaries],['current_product_gap','ordinary_round_robin']*3)
        self.assertEqual(len(set(seen)),6)

    def test_slow_priority_and_slow_ordinary_cannot_monopolize_time(self):
        clock=[0.0];seen=[]
        def slow(state,ref,**kw):seen.append(ref);clock[0]+=11;return {'request_id':ref,'outcome':'waiting'}
        with patch.object(ma.time,'monotonic',side_effect=lambda:clock[0]):summaries=self.ticks(4,slow)
        self.assertEqual([s['served']for s in summaries],[1]*4)
        self.assertEqual([s['results'][0]['selection_class']for s in summaries],['current_product_gap','ordinary_round_robin']*2)
        self.assertTrue(all(s['out_of_time']for s in summaries))
        self.assertEqual(len(set(seen)),4)

    def test_repeated_new_priority_does_not_reset_ordinary_cursor(self):
        seen=[]
        def serve(state,ref,**kw):seen.append(ref);return {'request_id':ref,'outcome':'waiting'}
        for n in range(4):
            self.urgent.append(self.fx.request(need={'kind':'answer','question':f'New high {n}'},lane='company_model_forecast'))
            self.ticks(1,serve)
        self.assertTrue({r['id']for r in self.normal}.issubset(seen))

    def test_wait_budget_refusal_error_and_reopen_keep_rotation_and_caps(self):
        seen=[];allows=[]
        def serve(state,ref,*,allow_costed,**kw):
            seen.append(ref);allows.append(allow_costed)
            if ref==self.urgent[0]['id']:raise RuntimeError('sensitive source text must not appear')
            return {'request_id':ref,'outcome':'budget_blocked','budget':{'scope':'pool'}}
        summaries=self.ticks(3,serve)
        self.assertTrue({r['id']for r in self.urgent}.issubset(seen))
        self.assertNotIn('sensitive',canonical_json([r for s in summaries for r in s['results']]))
        self.assertTrue(any(r.get('error_type')=='RuntimeError'for s in summaries for r in s['results']))
        self.assertEqual(allows[:4],[True,False,False,False])
        before=mr.request(self.state,self.urgent[0]['id']);mr.close(self.state,before['id'],status='exhausted',reason='closed',now=fixtures.NOW)
        mr.reopen(self.state,before['id'],reason='new material',now=fixtures.NOW)
        self.ticks(1,serve)
        self.assertEqual(ma.request_spend(self.state,before['id']),{'calls':0,'cost_micros':0})

    def test_priority_cohort_never_consumes_the_three_ordinary_slots(self):
        for n in range(20):
            self.urgent.append(self.fx.request(need={'kind':'answer','question':f'High priority incoming {n}'},lane='initial_screen'))
        summaries=self.ticks(4,lambda s,r,**kw:{'request_id':r,'outcome':'waiting'})
        normal_ids={r['id']for r in self.normal}
        for summary in summaries:
            ordinary=[r for r in summary['results']if r['selection_class']=='ordinary_round_robin']
            self.assertEqual(len(ordinary),3)
            self.assertTrue(all(r['request_id']in normal_ids for r in ordinary))

    def test_closed_priority_anchor_does_not_restart_cohort(self):
        first=self.ticks(1,lambda s,r,**kw:{'request_id':r,'outcome':'waiting'})[0]
        anchor=next(r['request_id']for r in first['results']if r['selection_class']=='current_product_gap')
        mr.close(self.state,anchor,status='exhausted',reason='closed',now=fixtures.NOW)
        second=self.ticks(1,lambda s,r,**kw:{'request_id':r,'outcome':'waiting'})[0]
        ordered=sorted(self.urgent,key=lambda r:ma.priority_key(r,{}))
        self.assertEqual(next(r['request_id']for r in second['results']if r['selection_class']=='current_product_gap'),ordered[1]['id'])

    def test_deadline_expiring_during_priority_recheck_never_writes_cursors(self):
        ma.ensure_schema(self.state);clock=[0.0];request=self.urgent[0]
        def recheck(*args,**kw):clock[0]=11;return True
        with patch.object(ma.time,'monotonic',side_effect=lambda:clock[0]),patch.object(priority,'still_current',side_effect=recheck):
            claimed=ma._advance_priority_turn(self.state,request,{},expected_hash=None,expected_priority_hash=None,
                priority=True,flip_order=True,now=fixtures.NOW,deadline=10,priority_proof=proofs(self.urgent)[request['id']])
        self.assertIsNone(claimed)
        with ma._reader(self.state)as c:
            for table in ('material_acquisition_service_cursor','material_acquisition_priority_cursor'):
                self.assertEqual(c.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0)

    def test_joint_cursor_cas_loser_never_serves(self):
        with patch.object(priority,'priority_gaps',return_value=(proofs(self.urgent),{})), \
                patch.object(ma,'_advance_priority_turn',return_value=None),patch.object(ma,'serve')as serve:
            result=ma.tick(self.state,now=fixtures.NOW,convert=False)
        serve.assert_not_called();self.assertTrue(result['scheduling_busy'])
        chosen=self.urgent[0]
        with patch.object(priority,'still_current',return_value=True):
            first=ma._advance_priority_turn(self.state,chosen,{},expected_hash=None,expected_priority_hash=None,
                priority=True,flip_order=True,now=fixtures.NOW,deadline=time.monotonic()+10,priority_proof=proofs(self.urgent)[chosen['id']])
        self.assertIsNotNone(first)
        self.assertIsNone(ma._advance_priority_turn(self.state,self.normal[0],{},expected_hash=None,
            expected_priority_hash=None,priority=False,flip_order=True,now=fixtures.NOW,deadline=time.monotonic()+10))

    def test_concurrent_signal_and_no_signal_ticks_cannot_serve_same_request(self):
        ma.ensure_schema(self.state)
        first=sorted(self.normal+self.urgent,key=lambda r:ma.priority_key(r,{}))[0]
        barrier=threading.Barrier(2);seen=[]
        ordinary_claim,priority_claim=ma._advance_service_cursor,ma._advance_priority_turn
        def ordinary(*args,**kwargs):barrier.wait(timeout=3);return ordinary_claim(*args,**kwargs)
        def urgent(*args,**kwargs):barrier.wait(timeout=3);return priority_claim(*args,**kwargs)
        def eligibility(state,pending,**kwargs):
            return (proofs([first]),{}) if threading.current_thread().name.startswith('urgent') else ({},{})
        def run():return ma.tick(self.state,now=fixtures.NOW,convert=False,max_requests=1)
        with patch.object(priority,'priority_gaps',side_effect=eligibility),                 patch.object(priority,'still_current',return_value=True),                 patch.object(ma,'_advance_service_cursor',side_effect=ordinary),                 patch.object(ma,'_advance_priority_turn',side_effect=urgent),                 patch.object(ma,'serve',side_effect=lambda s,r,**kw:seen.append(r)or{'request_id':r,'outcome':'waiting'}):
            with ThreadPoolExecutor(1,thread_name_prefix='urgent')as a,ThreadPoolExecutor(1,thread_name_prefix='ordinary')as b:
                left,right=a.submit(run),b.submit(run)
                summaries=[left.result(timeout=5),right.result(timeout=5)]
        self.assertEqual(seen,[first['id']])
        self.assertEqual(sorted(s['served']for s in summaries),[0,1])
        self.assertTrue(any(s.get('scheduling_busy')for s in summaries))

    def test_no_signal_uses_all_four_original_slots_and_does_not_create_priority_row(self):
        with patch.object(priority,'priority_gaps',return_value=({},{})),patch.object(ma,'serve',side_effect=lambda s,r,**kw:{'request_id':r,'outcome':'waiting'}):
            result=ma.tick(self.state,now=fixtures.NOW,convert=False)
        self.assertEqual(result['served'],4)
        self.assertEqual([r['request_id']for r in result['results']],[r['id']for r in sorted(self.normal+self.urgent,key=lambda r:ma.priority_key(r,{}))[:4]])
        with ma._reader(self.state)as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM material_acquisition_priority_cursor').fetchone()[0],0)

    def test_actual_lane_exposes_bounded_safe_selection_and_error_type(self):
        from dalton_core.material_acquisition_lane import dispatch
        def serve(state,ref,**kw):raise RuntimeError('private raw source text and token must not be exposed')
        with patch.object(priority,'priority_gaps',return_value=({},{})),patch.object(ma,'serve',side_effect=serve):
            output=dispatch(SimpleNamespace(state_dir=self.state))
        self.assertEqual(len(output['selection']),4)
        self.assertTrue(all(r['outcome']=='error' and r['error_type']=='RuntimeError' for r in output['selection']))
        self.assertNotIn('private raw',canonical_json(output))
        self.assertNotIn('question',canonical_json(output))

    def test_expired_budget_cannot_claim_or_flip_priority_cursor(self):
        with patch.object(priority,'priority_gaps',return_value=(proofs(self.urgent),{})),patch.object(ma,'serve')as serve:
            with patch.object(ma.time,'monotonic',return_value=5):
                result=ma.tick(self.state,now=fixtures.NOW,convert=False,max_seconds=-1)
        serve.assert_not_called();self.assertTrue(result['out_of_time'])
        with ma._reader(self.state)as c:self.assertIsNone(c.execute("SELECT name FROM sqlite_master WHERE name='material_acquisition_priority_cursor'").fetchone())


class CurrentGapProofTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.state=Path(self.tmp.name)
        self.mission={'id':'mission-version:test:1','mission_ref':'mission:test','universe':[{'company_ref':fixtures.SYNW},{'company_ref':fixtures.OTHER}]}
        self.mission['content_hash']=content_hash(self.mission)
        self.connection=sqlite3.connect(self.state/'core.sqlite');self.addCleanup(self.connection.close)
        self.connection.executescript('''CREATE TABLE coverage_mission_versions(mission_version_id TEXT PRIMARY KEY,mission_ref TEXT,record_json TEXT,content_hash TEXT);
        CREATE TABLE coverage_mission_pointer(mission_ref TEXT PRIMARY KEY,mission_version_id TEXT);
        CREATE TABLE coverage_mission_company_model_specs(spec_id TEXT PRIMARY KEY,company_ref TEXT,mission_version_ref TEXT,state_hash TEXT,assessment TEXT,revenue_anchor_json TEXT,revenue_drivers_json TEXT,expense_lines_json TEXT,forecast_statements_json TEXT,operating_metrics_json TEXT,horizon_json TEXT,metadata_json TEXT,task_hash TEXT,model_profile_ref TEXT,work_order_ref TEXT,decided_by TEXT,created_at TEXT,content_hash TEXT);''')
        self.connection.execute('INSERT INTO coverage_mission_versions VALUES(?,?,?,?)',(self.mission['id'],self.mission['mission_ref'],canonical_json(self.mission),self.mission['content_hash']))
        self.connection.execute('INSERT INTO coverage_mission_pointer VALUES(?,?)',(self.mission['mission_ref'],self.mission['id']));self.connection.commit()

    def spec(self,company=fixtures.SYNW,*,number=1,intrinsic=True):
        body={'schema_version':'0.4','company_ref':company,'state_hash':content_hash(str(number)),'assessment':'Source-backed driver',
              'revenue_anchor_concept':'us-gaap:Revenues','revenue_drivers':[{'ref':'segment','label':'Source segment','kind':'segment','basis_concept':'us-gaap:Revenues',**({}if intrinsic else{'basis_identity':{'statement':'income','dimensions':{'axis':'member'}}})}],
              'expense_lines':[],'forecast_statements':[],'operating_metrics':[],'horizon':{},'decided_by':'automation:model','task_hash':'a'*64}
        wire={**body,'content_hash':content_hash(body),'spec_id':f'spec:{company}:{number}','mission_version_ref':self.mission['id'],'model_profile_ref':None,'work_order_ref':None,'created_at':f'2026-10-11T01:00:0{number}+00:00'}
        self.connection.execute('INSERT INTO coverage_mission_company_model_specs VALUES('+','.join('?'for _ in range(18))+')',
            (wire['spec_id'],company,wire['mission_version_ref'],wire['state_hash'],wire['assessment'],canonical_json(wire['revenue_anchor_concept']),canonical_json(wire['revenue_drivers']),'[]','[]','[]','{}',canonical_json({'schema_version':'0.4'}),wire['task_hash'],None,None,wire['decided_by'],wire['created_at'],wire['content_hash']));self.connection.commit();return wire

    def request(self,spec):
        return open_gap_requests(self.state,spec,{'periods':['2026-06-30']})[0]

    def read(self):return priority.priority_gaps(self.state,mr.open_requests(self.state),deadline=time.monotonic()+5)

    def test_current_formal_forecast_gap_is_strong_and_foreign_or_drifted_record_is_not(self):
        from tests import test_model_forecast_driver as fx
        from dalton_core.model_forecast_driver import ForecastModelAuthority
        from dalton_core.store import DaltonStore
        company=fx.ACN
        self.mission['universe'].append({'company_ref':company})
        self.mission['content_hash']=content_hash({k:v for k,v in self.mission.items()if k!='content_hash'})
        self.connection.execute('UPDATE coverage_mission_versions SET record_json=?,content_hash=?',
            (canonical_json(self.mission),self.mission['content_hash']));self.connection.commit()
        spec=fx.spec(cash='required')
        spec.update(schema_version='0.3',assessment='Source-bound legacy',decided_by='automation:model',task_hash='a'*64)
        spec['mission_version_ref']=self.mission['id']
        spec['content_hash']=content_hash({k:v for k,v in spec.items()if k not in {'spec_id','content_hash','mission_version_ref'}})
        self.connection.execute('INSERT INTO coverage_mission_company_model_specs VALUES('+','.join('?'for _ in range(18))+')',
            (spec['spec_id'],company,self.mission['id'],spec['state_hash'],spec['assessment'],None,
             canonical_json(spec['revenue_drivers']),canonical_json(spec['expense_lines']),canonical_json(spec['forecast_statements']),
             canonical_json(spec['operating_metrics']),canonical_json(spec['horizon']),canonical_json({'schema_version':'0.3'}),
             spec['task_hash'],None,None,spec['decided_by'],'2026-10-11T01:00:01+00:00',spec['content_hash']))
        self.connection.commit()
        store=DaltonStore(str(self.state/'core.sqlite'));self.addCleanup(store.close)
        model=ForecastModelAuthority(store).publish(fx.model(specification=spec,mission_version_ref=self.mission['id']))
        req=open_gap_requests(self.state,spec,{'periods':[model['forecast_policy']['latest_actual_end']]},record=model)[0]
        qualified,info=self.read()
        self.assertEqual(qualified[req['id']]['basis'],'current_formal_forecast_gap')
        self.assertEqual(qualified[req['id']]['forecast_hash'],model['content_hash'])
        request=mr.request(self.state,req['id'])
        foreign={**model,'spec_ref':'spec:other'}
        self.assertIsNone(priority._proof(request,self.mission,spec,foreign))
        # A malformed latest authority must not fall back to an older answer.
        store.connection.execute('DROP TRIGGER forecast_model_no_update')
        store.connection.execute('UPDATE forecast_model_versions SET content_hash=?',('0'*64,))
        self.assertEqual(self.read()[0],{})

    def test_exact_current_intrinsic_gap_multiple_companies(self):
        refs=[]
        for company in (fixtures.SYNW,fixtures.OTHER):refs.append(self.request(self.spec(company))['id'])
        qualified,info=self.read();self.assertEqual(set(qualified),set(refs));self.assertEqual(info['qualified'],2)
        self.assertTrue(all(x['basis']=='current_spec_intrinsic_gap'for x in qualified.values()))

    def test_real_claim_rechecks_current_source_and_rejects_changed_spec_without_cursor_write(self):
        spec=self.spec();request=self.request(spec);qualified,_=self.read();proof=qualified[request['id']]
        self.assertTrue(priority.still_current(self.state,proof,deadline=time.monotonic()+5))
        with patch.object(ma,'serve',side_effect=lambda state,ref,**kw:{'request_id':ref,'outcome':'waiting'}):
            first=ma.tick(self.state,now=fixtures.NOW,convert=False)
        self.assertEqual(first['results'][0]['selection_class'],'current_product_gap')
        self.spec(number=2,intrinsic=False)
        self.assertFalse(priority.still_current(self.state,proof,deadline=time.monotonic()+5))
        with ma._reader(self.state)as c:
            normal=c.execute('SELECT content_hash FROM material_acquisition_service_cursor').fetchone()
            urgent=c.execute('SELECT content_hash FROM material_acquisition_priority_cursor').fetchone()
        self.assertIsNone(ma._advance_priority_turn(self.state,mr.request(self.state,request['id']),{},
            expected_hash=None if normal is None else normal[0],expected_priority_hash=urgent[0],priority=True,
            flip_order=True,now=fixtures.NOW,deadline=time.monotonic()+5,priority_proof=proof))
        with ma._reader(self.state)as c:self.assertEqual(c.execute('SELECT content_hash FROM material_acquisition_priority_cursor').fetchone()[0],urgent[0])

    def test_current_executed_model_is_not_reprioritized_by_old_missing_identity_heuristic(self):
        spec=self.spec();req=self.request(spec);view=mr.request(self.state,req['id'])
        self.assertIsNotNone(priority._proof(view,self.mission,spec,None))
        current={'id':'forecast:current','company_ref':spec['company_ref'],'spec_ref':spec['spec_id'],
                 'spec_hash':spec['content_hash'],'mission_version_ref':self.mission['id'],'content_hash':'b'*64,
                 'forecast_policy':{'latest_actual_end':view['period']},'results':[],
                 'execution_contract':{'drivers':{'status':'executed'}}}
        self.assertIsNone(priority._proof(view,self.mission,spec,current))

    def test_latest_spec_replacement_and_wrong_mission_do_not_get_priority(self):
        old=self.spec();req=self.request(old);self.spec(number=2,intrinsic=False)
        self.assertEqual(self.read()[0],{})
        self.connection.execute('UPDATE coverage_mission_company_model_specs SET mission_version_ref=?',('old-mission',));self.connection.commit()
        self.assertEqual(self.read()[0],{})

    def test_tampered_authority_and_closed_request_keep_ordinary_path(self):
        spec=self.spec();req=self.request(spec);mr.close(self.state,req['id'],status='exhausted',reason='no source',now=fixtures.NOW)
        self.assertEqual(priority.priority_gaps(self.state,[mr.request(self.state,req['id'])],deadline=time.monotonic()+5)[0],{})
        mr.reopen(self.state,req['id'],reason='new material',now=fixtures.NOW)
        self.assertIn(req['id'],self.read()[0])
        self.connection.execute('UPDATE coverage_mission_company_model_specs SET content_hash=?',('0'*64,));self.connection.commit()
        self.assertEqual(self.read()[0],{})

    def test_requester_string_without_matching_gap_and_no_current_record_is_not_priority(self):
        spec=self.spec(intrinsic=False)
        fake={**spec,'revenue_drivers':[{'ref':'segment','label':'Source segment','kind':'segment','basis_concept':'us-gaap:Revenues'}]}
        self.request(fake);self.assertEqual(self.read()[0],{})
        request=mr.open_requests(self.state)[0];wrong=deepcopy(request);wrong['acceptance']['economic_driver_identities'][0]['label']='forged'
        self.assertIsNone(priority._proof(wrong,self.mission,fake,None))
