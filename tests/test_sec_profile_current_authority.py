"""Held issuer facts survive revisions; source permission never does."""
import copy
import unittest
from datetime import date, datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.mission_source_discovery import MissionSourceDiscoveryCoordinator, DiscoveryLaunchRejected
from dalton_core.sec_issuer_reporting_profile import current_profile, SecReportingProfileError, SecReportingProfileUnavailable
from dalton_core.sec_plan_specs import effective_sec_plan
from tests import test_sec_issuer_reporting_profile as prior
from tests import test_sec_filings_index_discovery as fixture


class CurrentAuthorityTests(unittest.TestCase):
    setUp = fixture.SecFilingsIndexDiscoveryTests.setUp
    run_once = fixture.SecFilingsIndexDiscoveryTests.run_once
    body = prior.ForeignProfileTests.body
    adapter = prior.ForeignProfileTests.adapter
    root = prior.ForeignProfileTests.root
    profile = prior.ForeignProfileTests.profile
    launch = prior.ForeignProfileTests.launch

    def revise(self, *, change=None):
        authority = CoverageMissionAuthority(self.core)
        previous = self.mission
        keys = ('title','objective','industry_ref','universe','research_questions',
                'deliverables','source_plan','bindings','autonomy','budget')
        params = {k:copy.deepcopy(previous[k]) for k in keys}
        params['objective'] += ' Reviewed current source scope.'
        if change is not None:
            change(params)
        self.mission = authority.create_mission(previous['mission_ref'], **params,
            actor_ref=fixture.OWNER, version_id=previous['id']+':next',
            prior_version_ref=previous['id'], idempotency_key='profile-revision:'+previous['id'])
        return previous

    def coordinator(self):
        launcher = Mock();launcher.running.return_value=False
        launcher.start.return_value={'id':'discovery:synthetic'}
        budget=Mock(return_value={'remaining':1})
        co=SimpleNamespace(search_launcher=launcher,missions=CoverageMissionAuthority(self.core),
            store=self.core,source_ref='source:sec-edgar',spool_dir=self.state/'connector-spool',
            plan=effective_sec_plan(fixture.sec_plan()),clock=lambda:datetime.now(timezone.utc),
            _health_hold=lambda:None,_spec_block=lambda *a:None,_budget=budget,
            _missing_periods=lambda *a:[],_continuation_page=lambda *a,**k:None)
        return co,launcher,budget

    def test_revision_reuses_exact_old_facts_but_dispatch_and_child_bind_new_authority(self):
        profile=self.root();previous=self.revise()
        held=self.core.connection.execute('SELECT record_json FROM coverage_mission_source_discoveries WHERE record_id=?',
            (profile['discovery_ref'],)).fetchone()[0]
        self.assertEqual(self.profile(),profile)
        co,launcher,budget=self.coordinator()
        chosen=MissionSourceDiscoveryCoordinator.launch_discovery(co)
        self.assertEqual(chosen['spec_ref'],'annual-report-40f')
        self.assertEqual(launcher.start.call_args.kwargs['authorization']['mission_version_ref'],self.mission['id'])
        self.assertEqual(launcher.start.call_args.kwargs['reporting_profile'],profile)
        self.assertEqual(budget.call_count,1)
        _,ticket,spawn=self.launch(profile)
        self.assertEqual(ticket['mission_version_ref'],self.mission['id'])
        result=self.run_once(plan=effective_sec_plan(fixture.sec_plan()),spec_ref='annual-report-40f',
            as_of=date.today(),reporting_profile=profile,adapter=self.adapter(self.body()))
        self.assertEqual(result['status'],'succeeded',result['failure_reason'])
        self.assertEqual(result['authorization']['mission_version_ref'],self.mission['id'])
        self.assertEqual(result['parameters']['form'],'40-F')
        self.assertEqual(result['provider_calls'],1) # One isolated fake SEC response, not a model or live HTTP.
        self.assertEqual(self.core.connection.execute('SELECT record_json FROM coverage_mission_source_discoveries WHERE record_id=?',
            (profile['discovery_ref'],)).fetchone()[0],held)
        self.assertNotEqual(previous['id'],self.mission['id'])

    def test_withdrawn_source_or_write_scope_does_not_inherit_old_permission(self):
        for kind in ('source','write'):
            with self.subTest(kind=kind):
                profile=self.root()
                def remove(params):
                    if kind=='source':
                        next(s for s in params['source_plan'] if s['source_ref']=='source:sec-edgar')['status']='not_connected'
                    else:params['autonomy']['may_write'].remove('source_discovery')
                self.revise(change=remove)
                with self.assertRaisesRegex(SecReportingProfileError,'current SEC discovery authorization rejected'):
                    self.profile()
                with self.assertRaises(Exception):self.launch(profile)
                # Restore only via another formally signed fixture revision for the next subcase.
                self.revise(change=lambda p:(next(s for s in p['source_plan'] if s['source_ref']=='source:sec-edgar').update(status='connected'),
                    p['autonomy']['may_write'].append('source_discovery') if 'source_discovery' not in p['autonomy']['may_write'] else None))

    def test_stale_mission_wrong_company_or_issuer_never_rebind(self):
        profile=self.root();previous=self.revise()
        for params in ({'mission_version_ref':previous['id']}, {'company_ref':'company:outside'}, {'issuer':'0000000001'}):
            kwargs={'mission_version_ref':self.mission['id'],'company_ref':fixture.ACN,'issuer':fixture.CIK,'as_of':date.today(),**params}
            with self.assertRaises((SecReportingProfileError,ValueError)):
                current_profile(self.core.connection,self.spool,**kwargs)
        self.assertEqual(self.profile(),profile)

    def test_newer_reporting_regime_stale_raw_and_rehashed_false_profile_stay_closed(self):
        profile=self.root();self.revise()
        with self.assertRaises(SecReportingProfileUnavailable):
            current_profile(self.core.connection,self.spool,mission_version_ref=self.mission['id'],
                company_ref=fixture.ACN,issuer=fixture.CIK,as_of=date.today()+timedelta(days=91))
        self.root('10-K')
        with self.assertRaisesRegex(DiscoveryLaunchRejected,'no longer current'):self.launch(profile)
        self.last_spawn.assert_not_called()

    def test_repeated_revision_and_restart_preserve_profile_identity_and_budget_path(self):
        profile=self.root('20-F');self.revise();self.revise()
        for _ in range(2):
            self.assertEqual(self.profile(),profile)
            co,launcher,budget=self.coordinator();co._budget=lambda cap:{'remaining':0}
            result=MissionSourceDiscoveryCoordinator.launch_discovery(co)
            self.assertEqual(result['status'],'budget_exhausted');launcher.start.assert_not_called()
        self.assertEqual(self.core.connection.execute('SELECT COUNT(*) FROM coverage_mission_source_discoveries').fetchone()[0],1)

    def test_open_native_query_cadence_survives_revision_without_repeat(self):
        self.root();co,launcher,budget=self.coordinator()
        first=MissionSourceDiscoveryCoordinator.launch_discovery(co)
        self.assertEqual(first['spec_ref'],'annual-report-40f')
        self.revise();co,launcher,budget=self.coordinator()
        spec=next(s for s in co.plan['specs'] if s['spec_ref']==first['spec_ref'])
        self.assertEqual(MissionSourceDiscoveryCoordinator._cadence_block(
            co,self.mission['id'],fixture.ACN,spec),'previous discovery still open')
        launcher.start.assert_not_called()

    def test_human_probe_keeps_existing_authority_but_automation_cannot_use_it(self):
        profile=self.root()
        self.revise(change=lambda p:next(s for s in p['source_plan'] if s['source_ref']=='source:sec-edgar').update(status='probe_only'))
        with self.assertRaisesRegex(SecReportingProfileError,'automation discovery requires connected'):
            self.profile()
        self.assertEqual(current_profile(self.core.connection,self.spool,mission_version_ref=self.mission['id'],
            company_ref=fixture.ACN,issuer=fixture.CIK,as_of=date.today(),requested_by=fixture.OWNER),profile)
        result=self.run_once(plan=effective_sec_plan(fixture.sec_plan()),spec_ref='annual-report-40f',
            requested_by=fixture.OWNER,as_of=date.today(),reporting_profile=profile,
            adapter=self.adapter(self.body()))
        self.assertEqual(result['status'],'succeeded',result['failure_reason'])
        self.assertEqual(result['authorization']['requested_by'],fixture.OWNER)
