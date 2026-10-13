"""An archive page costs its own governed Work and keeps exact official provenance."""
import copy,json,unittest
from datetime import date
from dalton_core import material_requests as mr,material_acquisition as ma
from dalton_core.material_targeted_sec import targeted_sec_parameters,pending_archive_query
from dalton_core.sec_submissions_pages import ROOT_CURSOR,archive_cursor,continuation
from dalton_core.sec_public_adapter import normalize_sec_submissions,SecPublicAdapterError,SecPublicHttpAdapter
from dalton_core.public_http_transport import PublicHttpTransport
from dalton_core.sec_filings_index_cli import run_discovery
from dalton_core.store import content_hash,DaltonStore
from tests import test_sec_filings_index_discovery as fixture

class SecArchivePagesTests(unittest.TestCase):
    setUp=fixture.SecFilingsIndexDiscoveryTests.setUp
    def setup_query(self):
        self.plan=copy.deepcopy(fixture.sec_plan());self.plan['specs'][0]['lookback_days']=1095;self.plan.pop('content_hash');self.plan['content_hash']=content_hash(self.plan)
        self.request=mr.open_request(self.state,requester={'lane':'statement_context','section':'exact_xml_units'},subject=fixture.ACN,
            need={'kind':'document','doc_kinds':['sec_10k']},acceptance={'accession':'0001467373-24-000049','cik':fixture.CIK,
                'document_ref':'sec:filing:0001467373-24-000049','date_from':'2024-04-25','date_to':'2024-04-25'},
            source_priority=['sec_edgar'],now='2026-09-08T00:00:00+00:00')
        self.query=ma.enqueue_targeted_query(self.state,request_id=self.request['id'],source='sec_edgar',company_ref=fixture.ACN,
            spec_ref='annual-report-10k',phrasings=ma.build_queries(self.request,ma.identity_for(self.state,fixture.ACN),now='2026-09-08T00:00:00+00:00'),now='2026-09-08T00:00:00+00:00')
        self.params,self.binding=targeted_sec_parameters(self.state,plan=self.plan,company_ref=fixture.ACN,spec_ref='annual-report-10k',as_of=date(2026,9,8),
            mission_version_ref=self.mission['id'],mission_version_hash=self.mission['content_hash'],query_ref=self.query['query_id'])
        self.root=json.loads(fixture.submissions_body());self.root['filings']['files']=[{'name':'CIK'+fixture.CIK+'-submissions-001.json','filingFrom':'2020-01-01','filingTo':'2025-08-03','filingCount':100}]
        self.archive={'accessionNumber':['0001467373-24-000049'],'form':['10-K'],'filingDate':['2024-04-25'],'primaryDocument':['synthetic-20240331.htm']}
        self.urls=[]
        def exchange(request,*args,**kwargs):
            self.urls.append(request.url)
            body=self.archive if 'submissions-001' in request.url else self.root
            return fixture._Response(json.dumps(body).encode())
        self.adapter=SecPublicHttpAdapter(transport=PublicHttpTransport(resolver=lambda *_:('93.184.216.34',),exchange=exchange))
    def run_page(self,cursor=None,adapter=None):
        return run_discovery(state_dir=self.state,governance=self.governance,plan=self.plan,company_ref=fixture.ACN,
            spec_ref='annual-report-10k',requested_by=self.principal,mission_version_ref=self.mission['id'],mission_version_hash=self.mission['content_hash'],
            as_of=date(2026,9,8),summary_dir=self.state/('archive' if cursor else 'root'),adapter=adapter or self.adapter,targeted_binding=self.binding,cursor=cursor)
    def test_two_distinct_formal_works_one_http_each_and_fetchable_archive_url(self):
        self.setup_query();first=self.run_page();self.assertEqual((first['status'],first['provider_calls']),('succeeded',1),first['failure_reason'])
        self.assertEqual(first['filings'],[]);token=first['next_cursor'];self.assertTrue(token)
        second=self.run_page(token);self.assertEqual((second['status'],second['provider_calls']),('succeeded',1),second['failure_reason'])
        self.assertEqual(second['filings'][0]['accession'],'0001467373-24-000049');self.assertIn('/1467373/000146737324000049/synthetic-20240331.htm',second['filings'][0]['canonical_url'])
        self.assertEqual(len(self.urls),2);self.assertEqual(second['next_cursor'],None)
        from dalton_core.public_web_core_fetch import url_authority_from_discovery
        from dalton_core.raw_spool import RawSpoolReader
        s=DaltonStore(str(self.state/'core.sqlite'));self.addCleanup(s.close)
        a=url_authority_from_discovery(s.connection,RawSpoolReader(self.state/'connector-spool'),url_ref='sec:filing:0001467373-24-000049',source_envelope_ref=second['index']['source_envelope_ref'])
        self.assertEqual(a['canonical_url'],second['filings'][0]['canonical_url'])
        # The SEC connector's scheduler is independent from Core.
        import sqlite3
        with sqlite3.connect(self.state/'scheduler.sqlite') as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM scheduler_formal_results').fetchone()[0],2)
    def test_caller_cursor_without_durable_root_is_zero_transport(self):
        self.setup_query();token=continuation(self.root,self.params);r=self.run_page(token)
        self.assertEqual((r['status'],r['provider_calls']),('failed',0));self.assertEqual(self.urls,[]);self.assertIn('SecPageError',r['failure_reason'])
    def test_wrong_issuer_descriptor_duplicate_or_uncovered_date_reject(self):
        self.setup_query()
        for change in ('wrongissuer','duplicate','no_coverage'):
            p=copy.deepcopy(self.root)
            if change=='wrongissuer':p['filings']['files'][0]['name']='CIK0000000001-submissions-001.json'
            elif change=='duplicate':p['filings']['files'].append(copy.deepcopy(p['filings']['files'][0]))
            else:p['filings']['files'][0]['filingTo']='2023-12-31'
            with self.assertRaises(ValueError):normalize_sec_submissions(p,self.params,provider_status=200)
    def test_original_unpaged_contract_still_refuses_incomplete_recent_window(self):
        self.setup_query();p={k:v for k,v in self.params.items() if k!='cursor'}
        with self.assertRaisesRegex(SecPublicAdapterError,'outside SEC recent coverage'):normalize_sec_submissions(self.root,p,provider_status=200)
    def test_forged_root_hash_refuses_archive_before_transport(self):
        self.setup_query();first=self.run_page();self.assertEqual(first['status'],'succeeded',first['failure_reason']);token=first['next_cursor']
        from dalton_core.raw_spool import RawSpoolReader
        # A forged hash remains a well-shaped cursor but has no qualifying root.
        body=archive_cursor(token);body['root_payload_hash']='0'*64
        from dalton_core.store import canonical_json
        r=self.run_page(canonical_json(body));self.assertEqual(r['provider_calls'],0);self.assertEqual(len(self.urls),1)
    def test_normal_root_settlement_enables_one_continuation_and_rung_waits(self):
        self.setup_query();first=self.run_page();self.assertEqual(first['status'],'succeeded',first['failure_reason'])
        from unittest import mock
        from types import SimpleNamespace
        from datetime import datetime,timezone
        from dalton_core.coverage_mission import CoverageMissionAuthority
        from dalton_core.mission_source_discovery import MissionSourceDiscoveryCoordinator,discovery_query_hash
        s=DaltonStore(str(self.state/'core.sqlite'));self.addCleanup(s.close);missions=CoverageMissionAuthority(s)
        auth=missions.authorize_source_discovery(company_ref=fixture.ACN,source_ref='source:sec-edgar',requested_by=self.principal,
            mission_version_ref=self.mission['id'],mission_version_hash=self.mission['content_hash'])
        d=missions.record_discovery_dispatch(authorization=auth,discovery_plan_ref=self.plan['id'],discovery_plan_hash=self.plan['content_hash'],
            spec_ref='annual-report-10k',query_hash=discovery_query_hash(self.plan,self.params),ticket_ref='sec-filings-index:syntheticroot')
        ticket={'id':'sec-filings-index:syntheticroot','status':'succeeded','as_of':'2026-09-08','cursor':None,'targeted_binding':self.binding,
            'mission_version_ref':self.mission['id'],'mission_version_hash':self.mission['content_hash'],'summary':first}
        launcher=mock.Mock(state_dir=self.state);launcher.running.return_value=False;launcher.status.return_value=ticket
        fake=SimpleNamespace(store=s,missions=missions,search_launcher=launcher,source_ref='source:sec-edgar',plan=self.plan,
            clock=lambda:datetime(2026,9,8,tzinfo=timezone.utc),_health_hold=lambda:None,_budget=lambda cap:{'remaining':1},_missing_periods=lambda *args:[])
        result=MissionSourceDiscoveryCoordinator.settle_dispatches(fake);self.assertEqual(result[0]['status'],'succeeded')
        q=pending_archive_query(self.state,s.connection,self.plan,launcher);self.assertEqual(q['archive_cursor'],first['next_cursor'])
        directory=self.state/'sec-filings-index-runs'/'syntheticroot';directory.mkdir(parents=True)
        (directory/'ticket.json').write_text(json.dumps({k:v for k,v in ticket.items() if k!='summary'}));(directory/'summary.json').write_text(json.dumps(first))
        context=ma.RungContext(self.state,self.request,ma.identity_for(self.state,fixture.ACN),[],
            '2026-09-08T00:05:00+00:00',{'created_at':'2026-09-08T00:00:00+00:00','query':{'targeted_query':self.query['query_id']}})
        self.assertEqual(ma.TargetedSourceRung('sec_edgar').settle(context).outcome,'waiting')
        from dataclasses import replace
        expired = replace(context, now='2026-09-10T00:05:00+00:00')
        self.assertEqual(ma.TargetedSourceRung('sec_edgar').settle(expired).outcome, 'empty')

        # A normal busy/budget deferral must keep the same root continuation.
        ma.record_targeted_event(self.state,self.query['query_id'],status='deferred',kind='budget',reason='synthetic limit')
        self.assertEqual(pending_archive_query(self.state,s.connection,self.plan,launcher)['archive_cursor'],first['next_cursor'])
        launcher.start.return_value={'id':'sec-filings-index:syntheticarchive'}
        launched=MissionSourceDiscoveryCoordinator.launch_targeted_discovery(fake);self.assertEqual(launched['status'],'launched')
        self.assertEqual(launcher.start.call_args.kwargs['cursor'],first['next_cursor'])
        self.assertIsNone(pending_archive_query(self.state,s.connection,self.plan,launcher))
        # Even a failed archive may not reuse the successful root for an unbounded third call.
        launcher.status.return_value={**ticket,'cursor':first['next_cursor'],'status':'failed'}
        self.assertIsNone(pending_archive_query(self.state,s.connection,self.plan,launcher))

    def test_old_targeted_binding_does_not_rebuild_old_scope_as_paged(self):
        self.setup_query();legacy={k:v for k,v in self.binding.items() if k!='page_rule_ref'}
        p,b=targeted_sec_parameters(self.state,plan=self.plan,company_ref=fixture.ACN,spec_ref='annual-report-10k',as_of=date(2026,9,8),
            mission_version_ref=self.mission['id'],mission_version_hash=self.mission['content_hash'],binding=legacy,allow_closed=True)
        self.assertEqual(b,legacy);self.assertNotIn('cursor',p)
        saved=self.binding;self.binding=legacy
        old=self.run_page();self.binding=saved
        self.assertEqual(old['parameters'],p);self.assertNotIn('next_cursor',old)

        with self.assertRaises(ValueError):targeted_sec_parameters(self.state,plan=self.plan,company_ref=fixture.ACN,spec_ref='annual-report-10k',as_of=date(2026,9,8),
            mission_version_ref=self.mission['id'],mission_version_hash=self.mission['content_hash'],binding=legacy,cursor=continuation(self.root,self.params))

    def test_wrong_archive_cik_and_hidden_duplicate_accessions_fail_no_filing_discovery(self):
        self.setup_query();first=self.run_page();self.archive['cik']='0000000001'
        r=self.run_page(first['next_cursor']);self.assertEqual((r['status'],r['provider_calls']),('failed',1));self.assertEqual(r['filings'],[])
