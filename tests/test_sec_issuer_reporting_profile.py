import json
import sqlite3
import subprocess
import unittest
from datetime import date, datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from dalton_core import sec_issuer_reporting_profile as profiles
from dalton_core.acquired_sec_filings import _submissions_row
from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.mission_source_discovery import MissionSourceDiscoveryCoordinator, SecFilingsIndexLauncher, DiscoveryLaunchRejected
from dalton_core import mission_source_discovery as discovery
from dalton_core.public_http_transport import PublicHttpTransport
from dalton_core.raw_spool import RawSpoolReader
from dalton_core.sec_plan_specs import effective_sec_plan
from dalton_core.sec_public_adapter import SecPublicHttpAdapter
from dalton_core.store import DaltonStore, content_hash
from tests import test_sec_filings_index_discovery as fixture


class ForeignProfileTests(unittest.TestCase):
    setUp = fixture.SecFilingsIndexDiscoveryTests.setUp
    run_once = fixture.SecFilingsIndexDiscoveryTests.run_once

    def body(self, annual='40-F', *, issuer=fixture.CIK, other=None):
        today = date.today()
        rows = [(issuer+'-26-000001', annual, today.isoformat(), 'annual.htm'),
                (issuer+'-26-000002', '6-K', today.isoformat(), 'disclosure.htm'),
                (issuer+'-26-000003', '6-K/A', today.isoformat(), 'amendment.htm'),
                (issuer+'-20-000001', '6-K', (today-timedelta(days=1000)).isoformat(), 'old.htm')]
        if other:
            rows.append((issuer+'-26-000004', other, today.isoformat(), 'other.htm'))
        return json.dumps({'cik': issuer, 'filings': {'recent': {
            'accessionNumber': [r[0] for r in rows], 'form': [r[1] for r in rows],
            'filingDate': [r[2] for r in rows], 'primaryDocument': [r[3] for r in rows],
            'amendmentOf': [issuer+'-26-000002' if r[1]=='6-K/A' else None for r in rows]}}}).encode()

    def adapter(self, raw):
        return SecPublicHttpAdapter(transport=PublicHttpTransport(
            resolver=lambda *args: ('93.184.216.34',), exchange=lambda *a, **k: fixture._Response(raw)))

    def root(self, annual='40-F', *, raw=None):
        plan=fixture.sec_plan()
        sequence=getattr(self,'_root_sequence',0)
        self._root_sequence=sequence+1
        plan['specs'][0]['lookback_days']+=sequence
        plan['content_hash']=content_hash({k:v for k,v in plan.items() if k!='content_hash'})
        result = self.run_once(plan=plan,as_of=date.today(), adapter=self.adapter(raw or self.body(annual)))
        self.assertEqual(result['status'], 'succeeded', result['failure_reason'])
        self.core = DaltonStore(str(self.state/'core.sqlite'))
        self.addCleanup(self.core.close)
        self.spool = RawSpoolReader(self.state/'connector-spool')
        self.parent = result
        return self.profile()

    def profile(self):
        return profiles.current_profile(self.core.connection, self.spool, mission_version_ref=self.mission['id'],
            company_ref=fixture.ACN, issuer=fixture.CIK, as_of=date.today())

    def launch(self, profile, *, spec='annual-report-40f'):
        gp, pp = self.state/'governance.json', self.state/'plan.json'
        gp.write_text(json.dumps(self.governance.wire)); pp.write_text(json.dumps(fixture.sec_plan()))
        launcher = SecFilingsIndexLauncher(state_dir=self.state, governance_path=gp, plan_path=pp)
        auth = CoverageMissionAuthority(self.core).authorize_source_discovery(company_ref=fixture.ACN,
            source_ref='source:sec-edgar', requested_by=self.principal,
            mission_version_ref=self.mission['id'], mission_version_hash=self.mission['content_hash'])
        spawn=Mock(return_value=SimpleNamespace(pid=12345, poll=lambda: None));self.last_spawn=spawn
        with patch.object(discovery,'subprocess',SimpleNamespace(Popen=spawn,DEVNULL=subprocess.DEVNULL,STDOUT=subprocess.STDOUT)):
            ticket = launcher.start(authorization=auth, spec_ref=spec, as_of=date.today(), reporting_profile=profile)
        return launcher, ticket, spawn

    def test_empty_domestic_answer_proves_foreign_family_then_actual_native_query(self):
        profile = self.root()
        self.assertEqual(self.parent['document_count'], 0)
        self.assertEqual(profile['family'], 'foreign_40f')
        plan = effective_sec_plan(fixture.sec_plan())
        result = self.run_once(plan=plan, spec_ref='annual-report-40f', as_of=date.today(),
            reporting_profile=profile, adapter=self.adapter(self.body()))
        self.assertEqual(result['status'], 'succeeded', result['failure_reason'])
        self.assertEqual(result['parameters']['form'], '40-F')
        self.assertEqual(result['document_count'], 1)
        self.assertEqual(result['reporting_query_hash'], profiles.routing_query_hash(result['parameters'], profile))
        self.assertNotIn('quarter', result)
        identity = _submissions_row(self.state, self.core.connection, result['discovery_ref'], fixture.CIK+'-26-000001')
        self.assertEqual(identity['form'], '40-F')
        self.assertEqual(identity['foreign_company_ref'], fixture.ACN)

    def test_6k_disclosure_and_amendment_are_original_forms_not_quarters(self):
        profile = self.root()
        plan = effective_sec_plan(fixture.sec_plan())
        # A governed, explicit amendment query requires a provider revision
        # edge. It is not in the automatic candidate set.
        plan['specs'].append({'spec_ref':'foreign-disclosure-6k-amendment','form':'6-K/A',
            'lookback_days':90,'rediscovery_interval_days':2,'retry_interval_days':2})
        plan['content_hash']=content_hash({k:v for k,v in plan.items() if k!='content_hash'})
        for spec, form in [('foreign-disclosure-6k', '6-K'), ('foreign-disclosure-6k-amendment', '6-K/A')]:
            # The normal child requires a fresh strong profile, not a stale routing assertion.
            profile = self.profile()
            result = self.run_once(plan=plan, spec_ref=spec, as_of=date.today(), reporting_profile=profile,
                adapter=self.adapter(self.body()))
            self.assertEqual(result['status'], 'succeeded', result['failure_reason'])
            self.assertEqual(result['parameters']['form'], form)
            self.assertEqual({f['form'] for f in result['filings']}, {form})
        self.assertEqual(self.core.connection.execute('SELECT COUNT(*) FROM coverage_mission_statement_filings').fetchone()[0], 0)

    def test_coordinator_skips_domestic_and_other_foreign_family_before_budget(self):
        profile = self.root()
        plan = effective_sec_plan(fixture.sec_plan()); launcher = Mock(); launcher.running.return_value=False
        launcher.start.return_value={'id':'discovery:synthetic'}
        missions = CoverageMissionAuthority(self.core)
        budget = Mock(return_value={'remaining': 2})
        co = SimpleNamespace(search_launcher=launcher, missions=missions, store=self.core,
            source_ref='source:sec-edgar', spool_dir=self.state/'connector-spool', plan=plan,
            clock=lambda: datetime.now(timezone.utc), _health_hold=lambda: None,
            _spec_block=lambda *args: None, _budget=budget, _missing_periods=lambda *args: [],
            _continuation_page=lambda *a, **k: None)
        result=MissionSourceDiscoveryCoordinator.launch_discovery(co)
        self.assertEqual(result['status'], 'launched')
        self.assertEqual(result['spec_ref'], 'annual-report-40f')
        self.assertEqual(budget.call_count, 1)
        self.assertEqual(launcher.start.call_args.kwargs['reporting_profile'], profile)

    def test_owner_custom_spec_retains_existing_authorization_and_budget_path(self):
        self.root()
        plan=effective_sec_plan(fixture.sec_plan())
        plan['specs'].insert(0,{**plan['specs'][0], 'spec_ref':'owner-custom-report','form':'10-Q'})
        plan['content_hash']=content_hash({k:v for k,v in plan.items() if k!='content_hash'})
        launcher=Mock();launcher.running.return_value=False;launcher.start.return_value={'id':'custom'}
        budget=Mock(return_value={'remaining':1})
        co=SimpleNamespace(search_launcher=launcher,missions=CoverageMissionAuthority(self.core),store=self.core,
            source_ref='source:sec-edgar',spool_dir=self.state/'connector-spool',plan=plan,
            clock=lambda:datetime.now(timezone.utc),_health_hold=lambda:None,_spec_block=lambda *a:None,
            _budget=budget,_missing_periods=lambda *a:[],_continuation_page=lambda *a,**k:None)
        result=MissionSourceDiscoveryCoordinator.launch_discovery(co)
        self.assertEqual(result['spec_ref'],'owner-custom-report')
        self.assertNotIn('reporting_profile',launcher.start.call_args.kwargs)
        self.assertEqual(budget.call_count,1)

    def test_child_settlement_keeps_original_profile_and_refuses_routing_drift(self):
        p=self.root();plan=effective_sec_plan(fixture.sec_plan())
        summary=self.run_once(plan=plan,spec_ref='annual-report-40f',as_of=date.today(),
            reporting_profile=p,adapter=self.adapter(self.body()))
        self.assertEqual(summary['status'],'succeeded')
        dispatch={'dispatch_id':'dispatch:fixture','ticket_ref':'ticket:fixture','company_ref':fixture.ACN,
            'spec_ref':'annual-report-40f','query_hash':summary['query_hash']}
        ticket={'id':'ticket:fixture','status':'succeeded','summary':summary,'as_of':date.today().isoformat(),
            'reporting_profile':p,'reporting_query_hash':summary['reporting_query_hash']}
        missions=Mock();missions.open_discovery_dispatches.return_value=[dispatch]
        missions.settle_discovery_dispatch.side_effect=lambda ref,**kw:{'status':kw['status'],'failure_reason':kw.get('reason')}
        launcher=Mock();launcher.status.return_value=ticket
        co=SimpleNamespace(search_launcher=launcher,missions=missions,source_ref='source:sec-edgar',
            store=self.core,spool_dir=self.state/'connector-spool',plan=plan)
        # The completed native child is now the latest root. Settlement must
        # prove the original frozen parent instead of silently changing it.
        self.assertNotEqual(self.profile(),p)
        self.assertEqual(MissionSourceDiscoveryCoordinator.settle_dispatches(co)[0]['status'],'succeeded')
        ticket['reporting_query_hash']='0'*64
        self.assertEqual(MissionSourceDiscoveryCoordinator.settle_dispatches(co)[0]['status'],'failed')

    def test_ticket_command_and_digest_freeze_profile_and_query_identity(self):
        profile=self.root(); launcher, ticket, spawn=self.launch(profile)
        args=spawn.call_args.args[0]
        self.assertEqual(json.loads(args[args.index('--reporting-profile')+1]), profile)
        self.assertEqual(ticket['reporting_profile'], profile)
        self.assertEqual(len(ticket['reporting_query_hash']), 64)

    def test_changed_proof_and_missing_proof_refuse_before_spawn(self):
        profile=self.root()
        for proof in [None, {**profile, 'issuer':'0000000001'}, {**profile, 'family':'foreign_20f'},
                      {**profile, 'source_envelope_hash':'0'*64}]:
            with self.assertRaises(DiscoveryLaunchRejected):
                self.launch(proof)
            self.last_spawn.assert_not_called()

    def test_new_native_child_refuses_missing_or_rehashed_false_profile_zero_calls(self):
        profile=self.root(); altered={**profile, 'annual_descriptor':{**profile['annual_descriptor'], 'form':'20-F'}}
        altered['content_hash']=content_hash({k:v for k,v in altered.items() if k!='content_hash'})
        for proof in [None, altered]:
            result=self.run_once(plan=effective_sec_plan(fixture.sec_plan()), spec_ref='annual-report-40f',
                as_of=date.today(), reporting_profile=proof, adapter=self.adapter(self.body()))
            self.assertEqual((result['status'],result['provider_calls']), ('failed',0))

    def test_domestic_and_20f_are_generic_and_wrong_family_is_not_eligible(self):
        for annual, family in [('10-K','domestic'), ('20-F','foreign_20f')]:
            profile=self.root(annual)
            self.assertEqual(profile['family'],family)
            self.assertEqual(profiles.allows_form(profile,'10-K'), annual=='10-K')
            self.assertEqual(profiles.allows_form(profile,'20-F'), annual=='20-F')
            self.assertFalse(profiles.allows_form(profile,'40-F'))

    def test_conflicting_latest_families_do_not_guess_from_ticker(self):
        self.run_once(as_of=date.today(),adapter=self.adapter(self.body(other='10-K')))
        self.core=DaltonStore(str(self.state/'core.sqlite'));self.addCleanup(self.core.close)
        self.spool=RawSpoolReader(self.state/'connector-spool')
        with self.assertRaisesRegex(profiles.SecReportingProfileError,'conflict'): self.profile()

    def test_wrong_company_cik_stale_future_and_missing_original_refuse(self):
        profile=self.root()
        for company, issuer, at in [("company:other",fixture.CIK,date.today()),
                (fixture.ACN,'0000000001',date.today()),
                (fixture.ACN,fixture.CIK,date.today()+timedelta(days=91)),
                (fixture.ACN,fixture.CIK,date.today()-timedelta(days=1))]:
            with self.assertRaises(profiles.SecReportingProfileError):
                profiles.profile_from_discovery(self.core.connection,self.spool,discovery_ref=profile['discovery_ref'],
                    company_ref=company,issuer=issuer,as_of=at)
        bad=SimpleNamespace(read_object=lambda digest:b'corrupted')
        with self.assertRaisesRegex(profiles.SecReportingProfileError,'bytes'):
            profiles.verify_profile(self.core.connection,bad,profile,company_ref=fixture.ACN,issuer=fixture.CIK,as_of=date.today())

    def test_no_candidate_is_explicitly_missing_and_does_not_open_missing_spool(self):
        self.core=DaltonStore(str(self.state/'core.sqlite'));self.addCleanup(self.core.close)
        lazy=Mock(side_effect=AssertionError('no original needed when there is no candidate'))
        self.assertIsNone(profiles.current_profile(self.core.connection,lazy,mission_version_ref=self.mission['id'],
            company_ref=fixture.ACN,issuer=fixture.CIK,as_of=date.today()))
        lazy.assert_not_called()
        self.assertFalse(profiles.allows_form(None,'40-F'))
        self.assertTrue(profiles.allows_form(None,'10-K'))

    def test_other_company_same_generic_40f_route_and_foreign_domestic_mix(self):
        company='company:sec-cik:0001058290'; issuer='0001058290'
        plan=fixture.sec_plan();plan['companies']={company:{'cik':issuer}}
        plan['content_hash']=content_hash({k:v for k,v in plan.items() if k!='content_hash'})
        root=self.run_once(plan=plan,company_ref=company,as_of=date.today(),adapter=self.adapter(self.body(issuer=issuer)))
        self.assertEqual(root['status'],'succeeded',root['failure_reason'])
        self.core=DaltonStore(str(self.state/'core.sqlite'));self.addCleanup(self.core.close)
        p=profiles.current_profile(self.core.connection,RawSpoolReader(self.state/'connector-spool'),
            mission_version_ref=self.mission['id'],company_ref=company,issuer=issuer,as_of=date.today())
        result=self.run_once(plan=effective_sec_plan(plan),company_ref=company,spec_ref='annual-report-40f',
            as_of=date.today(),reporting_profile=p,adapter=self.adapter(self.body(issuer=issuer)))
        self.assertEqual(result['status'],'succeeded',result['failure_reason'])
        self.assertEqual(result['parameters']['issuer'],issuer)
        self.assertFalse(profiles.allows_form(p,'10-K'))

    def test_newer_domestic_parent_invalidates_foreign_ticket_before_spawn(self):
        old=self.root()
        current=self.root('10-K')
        self.assertEqual(current['family'],'domestic')
        with self.assertRaisesRegex(DiscoveryLaunchRejected,'no longer current'):
            self.launch(old)
        self.last_spawn.assert_not_called()

    def test_raw_original_hash_and_sql_projection_drift_do_not_authorize_profile(self):
        p=self.root()
        self.core.connection.execute('DROP TRIGGER coverage_mission_source_discoveries_no_update')
        self.core.connection.execute('UPDATE coverage_mission_source_discoveries SET company_ref=? WHERE record_id=?',
            ('company:other',p['discovery_ref']))
        self.assertIsNone(self.profile())
        with self.assertRaises(ValueError):
            profiles.verify_profile(self.core.connection,self.spool,p,company_ref=fixture.ACN,
                issuer=fixture.CIK,as_of=date.today())

    def test_missing_original_does_not_permanently_block_existing_index_bootstrap(self):
        profile=self.root(); launcher=Mock();launcher.running.return_value=False
        launcher.start.return_value={'id':'discovery:bootstrap'}
        co=SimpleNamespace(search_launcher=launcher, missions=CoverageMissionAuthority(self.core),store=self.core,
            source_ref='source:sec-edgar',spool_dir=self.state/'connector-spool',plan=effective_sec_plan(fixture.sec_plan()),
            clock=lambda:datetime.now(timezone.utc),_health_hold=lambda:None,_spec_block=lambda *a:None,
            _budget=lambda cap:{'remaining':1},_missing_periods=lambda *a:[],_continuation_page=lambda *a,**k:None)
        with patch.object(profiles,'current_profile',side_effect=profiles.SecReportingProfileUnavailable('original unavailable')):
            result=MissionSourceDiscoveryCoordinator.launch_discovery(co)
        self.assertEqual(result['spec_ref'],'annual-report-10k')
        self.assertNotIn('reporting_profile',launcher.start.call_args.kwargs)
        self.assertTrue(any('reporting_profile_missing' in r['reason']for r in result['skipped']))

    def test_foreign_profile_never_bypasses_original_health_authority_or_budget(self):
        profile=self.root(); launcher=Mock();launcher.running.return_value=False
        co=SimpleNamespace(search_launcher=launcher,missions=CoverageMissionAuthority(self.core),store=self.core,
            source_ref='source:sec-edgar',spool_dir=self.state/'connector-spool',plan=effective_sec_plan(fixture.sec_plan()),
            clock=lambda:datetime.now(timezone.utc),_health_hold=lambda:None,_spec_block=lambda *a:None,
            _budget=lambda cap:{'remaining':0},_missing_periods=lambda *a:[],_continuation_page=lambda *a,**k:None)
        self.assertEqual(MissionSourceDiscoveryCoordinator.launch_discovery(co)['status'],'budget_exhausted')
        launcher.start.assert_not_called()
        co._health_hold=lambda:{'status':'health_hold','reason':'held'}
        self.assertEqual(MissionSourceDiscoveryCoordinator.launch_discovery(co)['status'],'health_hold')
        launcher.start.assert_not_called()


if __name__ == '__main__': unittest.main()
