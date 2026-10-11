"""Exact request dates survive the SEC coordinator, launch, child and settlement."""
import json
import sqlite3
import unittest
from datetime import date,datetime,timezone
from types import SimpleNamespace
from unittest import mock
from dalton_core import material_acquisition as ma,material_requests as mr
from dalton_core.material_targeted_sec import targeted_sec_parameters,TargetedSecError
from dalton_core.mission_source_discovery import SecFilingsIndexLauncher,DiscoveryLaunchRejected,MissionSourceDiscoveryCoordinator,discovery_query_hash
from tests import test_sec_filings_index_discovery as fixture

class TargetedSecTests(unittest.TestCase):
    setUp=fixture.SecFilingsIndexDiscoveryTests.setUp
    run_once=fixture.SecFilingsIndexDiscoveryTests.run_once
    def queued(self,**changes):
        accept={'cik':fixture.CIK,'accession':'0001467373-25-000217','document_ref':'sec:filing:0001467373-25-000217',
                'date_from':'2025-10-10','date_to':'2025-10-10','min_passages':1,**changes}
        r=mr.open_request(self.state,requester={'lane':'statement_context','section':'exact_xml_units'},subject=fixture.ACN,
             need={'kind':'document','doc_kinds':['sec_10k']},acceptance=accept,source_priority=['indexed_corpus','sec_edgar'],
             period='2025-08-31',now='2026-09-06T00:00:00+00:00')
        phrases=ma.build_queries(r,ma.identity_for(self.state,fixture.ACN),now='2026-09-06T00:00:00+00:00')
        q=ma.enqueue_targeted_query(self.state,request_id=r['id'],source='sec_edgar',company_ref=fixture.ACN,
              spec_ref='annual-report-10k',phrasings=phrases,period=r['period'],now='2026-09-06T00:00:00+00:00')
        return r,q
    def params(self,q,**options):
        return targeted_sec_parameters(self.state,plan=fixture.sec_plan(),company_ref=fixture.ACN,spec_ref='annual-report-10k',
             as_of=date(2026,9,8),mission_version_ref=self.mission['id'],mission_version_hash=self.mission['content_hash'],
             **({'query_ref':q['query_id']} if 'binding' not in options else {}),**options)
    def launcher(self):
        self.governance_path=self.state/'governance.json';self.governance_path.write_text(json.dumps(self.governance.wire))
        self.plan_path=self.state/'plan.json';self.plan_path.write_text(json.dumps(fixture.sec_plan()))
        return SecFilingsIndexLauncher(state_dir=self.state,governance_path=self.governance_path,plan_path=self.plan_path)
    def auth(self):
        from dalton_core.store import DaltonStore
        from dalton_core.coverage_mission import CoverageMissionAuthority
        s=DaltonStore(str(self.state/'core.sqlite'))
        try:return CoverageMissionAuthority(s).authorize_source_discovery(company_ref=fixture.ACN,source_ref='source:sec-edgar',
                    requested_by=self.principal,mission_version_ref=self.mission['id'],mission_version_hash=self.mission['content_hash'])
        finally:s.close()
    def test_exact_date_reaches_real_index_work_and_formal_discovery(self):
        r,q=self.queued();p,b=self.params(q);before=r['content_hash']
        result=self.run_once(targeted_binding=b)
        self.assertEqual((result['status'],result['provider_calls']),('succeeded',1),result['failure_reason'])
        self.assertEqual(result['parameters'],p);self.assertEqual(result['targeted_binding'],b)
        self.assertEqual([(f['accession'],f['filing_date']) for f in result['filings']],[('0001467373-25-000217','2025-10-10')])
        c=sqlite3.connect(self.state/'core.sqlite')
        try:
            wire=json.loads(c.execute('SELECT record_json FROM coverage_mission_source_discoveries WHERE record_id=?',(result['discovery_ref'],)).fetchone()[0])
            self.assertEqual(wire['parameters'],p);self.assertEqual(wire['query_hash'],discovery_query_hash(fixture.sec_plan(),p))
        finally:c.close()
        self.assertEqual(mr.request(self.state,r['id'])['content_hash'],before)
    def test_corrupt_exact_identity_and_outside_window_refuse_zero_calls(self):
        for changes in ({'cik':'0000000001'},{'document_ref':'sec:filing:0001467373-25-000001'},
                        {'date_from':'2020-10-10','date_to':'2020-10-10'},{'date_to':'2025-10-11'}):
            r,q=self.queued(**changes)
            with self.assertRaises(TargetedSecError):self.params(q)
    def test_changed_binding_refuses_before_spawn(self):
        r,q=self.queued();_,b=self.params(q)
        with mock.patch('subprocess.Popen') as spawn,self.assertRaises(DiscoveryLaunchRejected):
            self.launcher().start(authorization=self.auth(),spec_ref='annual-report-10k',as_of=date(2026,9,8),
                                  targeted_binding={**b,'request_hash':'0'*64})
        spawn.assert_not_called()
    def test_closed_request_no_call_but_historical_binding_replays(self):
        r,q=self.queued();p,b=self.params(q);mr.close(self.state,r['id'],status='found',reason='synthetic local original')
        result=self.run_once(targeted_binding=b)
        self.assertEqual((result['status'],result['provider_calls']),('failed',0));self.assertIn('no longer active',result['failure_reason'])
        self.assertEqual(self.params(q,binding=b,allow_closed=True),(p,b))
    def test_coordinator_passes_bound_parameters_and_settlement_rejects_drift(self):
        r,q=self.queued();p,b=self.params(q);launcher=mock.Mock(state_dir=self.state);launcher.running.return_value=False
        launcher.start.return_value={'id':'sec-filings-index:synthetic'}
        missions=mock.Mock();missions.active_mission.return_value=self.mission;missions.authorize_source_discovery.return_value=self.auth()
        missions.record_discovery_dispatch.return_value={'dispatch_id':'dispatch:synthetic'}
        fake=SimpleNamespace(search_launcher=launcher,missions=missions,source_ref='source:sec-edgar',plan=fixture.sec_plan(),
             clock=lambda:datetime(2026,9,8,tzinfo=timezone.utc),_health_hold=lambda:None,_budget=lambda cap:{'remaining':1},_missing_periods=lambda *args:[])
        result=MissionSourceDiscoveryCoordinator.launch_targeted_discovery(fake)
        self.assertEqual(result['status'],'launched');self.assertEqual(launcher.start.call_args.kwargs['targeted_binding'],b)
        self.assertEqual(missions.record_discovery_dispatch.call_args.kwargs['query_hash'],discovery_query_hash(fake.plan,p))
        d={'dispatch_id':'dispatch:synthetic','ticket_ref':'sec-filings-index:synthetic','company_ref':fixture.ACN,'spec_ref':'annual-report-10k',
           'query_hash':discovery_query_hash(fake.plan,p),'mission_version_ref':self.mission['id'],'mission_version_hash':self.mission['content_hash']}
        missions.open_discovery_dispatches.return_value=[d];missions.settle_discovery_dispatch.return_value={'status':'failed'}
        launcher.status.return_value={'status':'succeeded','as_of':'2026-09-08','targeted_binding':b,
             'summary':{'targeted_binding':b,'parameters':{**p,'date_from':'2025-08-04'},'query_hash':d['query_hash'],'discovery_ref':'synthetic'}}
        self.assertEqual(MissionSourceDiscoveryCoordinator.settle_dispatches(fake)[0]['status'],'failed')
    def test_sql_projection_drift_is_not_a_free_parameter_override(self):
        r,q=self.queued();c=sqlite3.connect(mr.ledger_path(self.state));c.execute('DROP TRIGGER material_targeted_queries_no_update')
        c.execute('UPDATE material_targeted_queries SET company_ref=? WHERE query_id=?',('company:other',q['query_id']));c.commit();c.close()
        with self.assertRaisesRegex(TargetedSecError,'identity'):self.params(q)
