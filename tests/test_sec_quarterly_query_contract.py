from __future__ import annotations
import copy,json,unittest
from datetime import date
from dalton_core.sec_plan_specs import effective_sec_plan,generator_specs,FOREIGN_SPECS
from dalton_core.mission_source_discovery import build_discovery_parameters,validate_discovery_plan
from dalton_core.material_targeted_sec import targeted_sec_parameters,TargetedSecError
from dalton_core import material_requests as mr,material_acquisition as ma
from dalton_core.store import content_hash
from tests import test_sec_filings_index_discovery as fixture

class QuarterlySpecTests(unittest.TestCase):
    def test_missing_quarter_spec_added_without_mutating_original_or_budget(self):
        original=fixture.sec_plan();before=copy.deepcopy(original);plan=effective_sec_plan(original)
        specs={s['spec_ref']:s for s in plan['specs']}
        self.assertEqual(specs['quarterly-report-10q']['lookback_days'],1095)
        self.assertEqual(specs['quarterly-report-10q']['form'],'10-Q')
        self.assertEqual(original,before);self.assertEqual(plan['budget'],original['budget'])
        for spec in original['specs']:self.assertEqual(specs[spec['spec_ref']],spec)
        self.assertTrue(set(FOREIGN_SPECS)-{'annual-report-20f-amendment','annual-report-40f-amendment','foreign-disclosure-6k-amendment'} <= specs.keys())
        self.assertEqual(effective_sec_plan(plan),plan)

    def test_owner_quarterly_intervals_keep_1096_or_stricter(self):
        for days in (1096,20):
            original=fixture.sec_plan();body={k:v for k,v in original.items() if k!='content_hash'}
            spec={'spec_ref':'quarterly-report-10q','form':'10-Q','lookback_days':days,'rediscovery_interval_days':45,'retry_interval_days':5}
            body['specs']=body['specs']+[spec];original=validate_discovery_plan({**body,'content_hash':content_hash(body)})
            plan=effective_sec_plan(original);self.assertEqual(next(s for s in plan['specs']if s['spec_ref']==spec['spec_ref']),spec)
            self.assertEqual(plan['budget'],original['budget'])

    def test_new_generator_defaults_cover_three_actual_filed_dates(self):
        original=fixture.sec_plan();body={k:v for k,v in original.items()if k!='content_hash'};body['specs']=generator_specs()
        plan=validate_discovery_plan({**body,'content_hash':content_hash(body)})
        self.assertEqual(effective_sec_plan(plan),plan)
        params=build_discovery_parameters(plan,spec_ref='quarterly-report-10q',company_ref=fixture.ACN,as_of=date(2026,10,12))
        self.assertEqual(params['form'],'10-Q');self.assertEqual(params['issuer'],fixture.CIK)
        for filed in ('2025-02-27','2024-08-29','2024-05-30'):
            self.assertLessEqual(params['date_from'],filed);self.assertLessEqual(filed,params['date_to'])

class TargetedQuarterlyTests(unittest.TestCase):
    setUp=fixture.SecFilingsIndexDiscoveryTests.setUp

    def queued(self,filed):
        req=mr.open_request(self.state,requester={'lane':'statement_context','section':'exact_xml_units'},subject=fixture.ACN,
            need={'kind':'document','doc_kinds':['sec_10q']},acceptance={'cik':fixture.CIK,'accession':'0001234567-24-000001',
            'document_ref':'sec:filing:0001234567-24-000001','date_from':filed,'date_to':filed,'min_passages':1},
            source_priority=['indexed_corpus','sec_edgar'],period=filed,now='2026-10-12T00:00:00+00:00')
        q=ma.enqueue_targeted_query(self.state,request_id=req['id'],source='sec_edgar',company_ref=fixture.ACN,
            spec_ref='quarterly-report-10q',phrasings=ma.build_queries(req,ma.identity_for(self.state,fixture.ACN),now='2026-10-12T00:00:00+00:00'),
            period=req['period'],now='2026-10-12T00:00:00+00:00')
        return req,q

    def parameters(self,plan,q):
        return targeted_sec_parameters(self.state,plan=plan,company_ref=fixture.ACN,spec_ref='quarterly-report-10q',
            as_of=date(2026,10,12),mission_version_ref=self.mission['id'],mission_version_hash=self.mission['content_hash'],query_ref=q['query_id'])

    def test_current_request_and_frozen_exact_date_now_validate_without_provider(self):
        for filed in ('2025-02-27','2024-08-29','2024-05-30'):
            req,q=self.queued(filed);before=mr.request(self.state,req['id'])
            with self.assertRaises(TargetedSecError):self.parameters(fixture.sec_plan(),q)
            params,binding=self.parameters(effective_sec_plan(fixture.sec_plan()),q)
            self.assertEqual((params['date_from'],params['date_to'],params['form']),(filed,filed,'10-Q'))
            self.assertEqual(binding['request_hash'],req['content_hash']);self.assertEqual(mr.request(self.state,req['id']),before)

    def test_exact_date_outside_bounded_or_owner_window_still_refuses(self):
        req,q=self.queued('2020-05-30')
        with self.assertRaisesRegex(TargetedSecError,'window'):self.parameters(effective_sec_plan(fixture.sec_plan()),q)
        req,q=self.queued('2024-05-30');original=fixture.sec_plan();body={k:v for k,v in original.items()if k!='content_hash'}
        body['specs'].append({'spec_ref':'quarterly-report-10q','form':'10-Q','lookback_days':20,'rediscovery_interval_days':7,'retry_interval_days':2})
        plan=effective_sec_plan(validate_discovery_plan({**body,'content_hash':content_hash(body)}))
        with self.assertRaisesRegex(TargetedSecError,'window'):self.parameters(plan,q)
