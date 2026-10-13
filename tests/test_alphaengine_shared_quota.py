from __future__ import annotations
import json
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dalton_core.alphaengine_shared_quota import load_authority, refused_before_send, quota_increase_recovered
from dalton_core.connector import ConnectorQuotaExceeded
from dalton_core import host_source_budget_sync as sync
from dalton_core.host_research_settings import _write
from dalton_core.mission_source_discovery import build_discovery_parameters, MissionSourceDiscoveryCoordinator
from dalton_core.store import content_hash
from tests import test_host_source_budget_sync as fixtures
from tests.test_mission_source_discovery import SearchHarness, plan_for_tests, ACN, CTSH

class SharedQuotaTests(unittest.TestCase):
    def setUp(self):
        self.fx = fixtures.SourceBudgetSyncTests()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.h = SearchHarness(self.fx.state, [])
        self.addCleanup(self.h.close)
        self.plan = plan_for_tests()

    def authorize(self, cap=260):
        sync.apply(self.fx.server, self.fx.manager, self.fx.change(cap))
        return load_authority(self.fx.state)

    def parameters(self, company=ACN):
        return build_discovery_parameters(self.plan, company_ref=company,
            spec_ref='earnings-call-transcripts', as_of=self.h.clock().date())

    def exhaust(self):
        request = self.h.search.build_request(self.parameters())
        first = self.h.search.search(request)
        policy = self.h.search._authorities['rate_policy']
        for _ in range(48):
            self.h.clock.advance(seconds=1)
            self.h.search.search(self.h.search.build_request(self.parameters()))
        reservation = self.h.connectors.reserve_quota(first['connector_invocation_ref'], policy['id'], 2,
            {'calls':1, 'bytes':0, 'records':0, 'cost_micros':0}, ttl_seconds=1,
            idempotency_key='synthetic-unknown-49')
        self.h.clock.advance(seconds=2)
        refused = self.h.search.build_request(self.parameters())
        with self.assertRaises(ConnectorQuotaExceeded):
            self.h.search.search(refused)
        return refused, reservation, policy

    def test_two_operations_append_without_replacing_profiles_or_old_scope(self):
        for core, operation, units, multiplier in (
                (self.h.search,'search_library',50,1), (self.h.acquisition,'get_document',80,20)):
            old = core.ensure_governed_authorities()
            old_wire = json.dumps(old['rate_policy'], sort_keys=True)
            core.quota_authority = self.authorize()
            core._authorities = None
            new = core.ensure_governed_authorities()
            self.assertEqual(new['profile']['content_hash'],old['profile']['content_hash'])
            self.assertEqual(new['price']['content_hash'],old['price']['content_hash'])
            self.assertEqual(new['rate_policy']['limits']['calls'],260)
            self.assertEqual(new['rate_policy']['limits']['records'],
                260 if operation=='get_document' else 260*new['profile']['max_records'])
            self.assertEqual(new['rate_policy']['quota_scope_ref'],old['rate_policy']['quota_scope_ref'])
            self.assertEqual(new['rate_policy']['prior_version_ref'],old['rate_policy']['id'])
            saved = self.h.core.connection.execute('SELECT record_json FROM connector_rate_policy_versions '
                'WHERE policy_version_id=?',(old['rate_policy']['id'],)).fetchone()[0]
            self.assertEqual(json.loads(saved),{k:v for k,v in json.loads(old_wire).items() if k!='write_status'})

    def test_unknown_old_reservations_remain_counted_and_new_search_is_admitted(self):
        refused, reservation, old = self.exhaust()
        con=self.h.core.connection
        before=con.execute('SELECT record_json FROM connector_quota_reservations WHERE reservation_id=?',
                           (reservation['id'],)).fetchone()[0]
        self.h.search.quota_authority=self.authorize()
        self.h.search._authorities=None
        self.h.clock.advance(seconds=1)
        receipt=self.h.search.search(self.h.search.build_request(self.parameters(CTSH)))
        self.assertEqual(receipt['outcome'],'succeeded')
        self.assertEqual(before,con.execute('SELECT record_json FROM connector_quota_reservations '
            'WHERE reservation_id=?',(reservation['id'],)).fetchone()[0])
        self.assertIsNone(con.execute('SELECT 1 FROM connector_quota_settlements WHERE reservation_ref=?',
                                    (reservation['id'],)).fetchone())
        self.assertEqual(refused_before_send(con,'connector-runner-request:alphaengine-search:'+
            refused['request_hash'][:20])[1]['parameters'],refused['parameters'])

    def test_unrelated_revision_is_idempotent_lowering_keeps_same_scope_zero_denies(self):
        self.h.search.ensure_governed_authorities()
        self.h.search.quota_authority=self.authorize()
        self.h.search._authorities=None
        first=self.h.search.ensure_governed_authorities()['rate_policy']
        self.h.search.quota_authority=self.authorize()
        self.h.search._authorities=None
        self.assertEqual(first['content_hash'],self.h.search.ensure_governed_authorities()['rate_policy']['content_hash'])
        self.h.search.quota_authority=self.authorize(80)
        self.h.search._authorities=None
        lower=self.h.search.ensure_governed_authorities()['rate_policy']
        self.assertEqual(lower['quota_scope_ref'],first['quota_scope_ref'])
        self.assertEqual(lower['limits']['calls'],80)
        self.h.search.quota_authority=self.authorize(0)
        self.h.search._authorities=None
        with self.assertRaises(ConnectorQuotaExceeded): self.h.search.ensure_governed_authorities()

    def test_stale_forged_or_cross_environment_binding_cannot_raise_quota(self):
        self.h.search.ensure_governed_authorities()
        good=self.authorize()
        for key,val in [('owner_cap',2000),('state_dir',str(self.fx.root/'other'))]:
            forged={**good,key:val};forged['content_hash']=content_hash({k:v for k,v in forged.items() if k!='content_hash'})
            self.h.search.quota_authority=forged;self.h.search._authorities=None
            with self.assertRaises((ValueError,KeyError)): self.h.search.ensure_governed_authorities()
        self.authorize(300)
        self.h.search.quota_authority=good;self.h.search._authorities=None
        with self.assertRaises(ValueError): self.h.search.ensure_governed_authorities()
        config=json.loads(self.fx.service.read_bytes());config['core_db']=str(self.fx.root/'other.sqlite')
        _write(self.fx.service,config)
        with self.assertRaises(ValueError): load_authority(self.fx.state)

    def test_cli_reports_local_quota_without_provider_retry_or_fee_claim(self):
        from dalton_core.alphaengine_search_cli import run_discovery
        from tests.test_mission_source_discovery import approved_governance
        from unittest.mock import MagicMock
        self.authorize()
        fake=MagicMock();fake.search.side_effect=ConnectorQuotaExceeded('connector calls quota exceeded')
        with patch('dalton_core.coverage_mission.CoverageMissionAuthority.authorize_source_discovery',
                   return_value={'actor_ref':'automation:test'}), patch(
                   'dalton_core.alphaengine_search_cli.AlphaEngineCoreSearch',return_value=fake) as factory:
            result=run_discovery(state_dir=self.fx.state,governance=approved_governance(),plan=self.plan,
                company_ref=ACN,spec_ref='earnings-call-transcripts',requested_by='automation:test',
                mission_version_ref='mission:1',mission_version_hash='a'*64,as_of=self.h.clock().date(),
                handle=self.h.handle,transport='rehearsal',summary_dir=self.fx.state/'private-result')
        self.assertEqual(result['failure_detail']['code'],'LOCAL_QUOTA_EXHAUSTED')
        self.assertEqual(result['status'],'failed');self.assertEqual(result['provider_calls'],0)
        self.assertEqual(factory.call_args.kwargs['quota_authority']['owner_cap'],260)
        self.assertIsNone(result['search']);self.assertNotIn('cost_micros',result)

    def test_valid_other_environment_authority_is_not_accepted_by_this_core(self):
        self.h.search.ensure_governed_authorities()
        other=fixtures.SourceBudgetSyncTests();other.setUp();self.addCleanup(other.doCleanups)
        sync.apply(other.server,other.manager,other.change(260))
        self.h.search.quota_authority=load_authority(other.state);self.h.search._authorities=None
        with self.assertRaisesRegex(ValueError,'another Core'):
            self.h.search.ensure_governed_authorities()

    def test_refusal_barrier_tamper_or_reserved_transport_is_not_unsent(self):
        refused, _, _=self.exhaust()
        ref='connector-runner-request:alphaengine-search:'+refused['request_hash'][:20]
        request,_,_=refused_before_send(self.h.core.connection,ref)
        self.h.journal.append(ref,'reserved',{'reservation_ref':'synthetic:reservation'})
        with self.assertRaises(ValueError): refused_before_send(self.h.core.connection,ref)

    def test_current_quota_increase_removes_only_exact_local_refusal_cadence(self):
        refused,_,_=self.exhaust()
        ref='alphaengine-discovery:'+'a'*24
        directory=self.fx.state/'discoveries'/('a'*24);directory.mkdir(parents=True)
        auth={'company_ref':ACN,'source_ref':'source:alphaengine','actor_ref':'automation:test'}
        start=(self.h.clock()-timedelta(seconds=1)).isoformat(timespec='microseconds')
        done=self.h.clock().isoformat(timespec='microseconds')
        common={'company_ref':ACN,'spec_ref':'earnings-call-transcripts','requested_by':'automation:test'}
        dispatch={**common,'status':'failed','source_ref':'source:alphaengine','ticket_ref':ref,
            'discovery_plan_ref':self.plan['id'],'discovery_plan_hash':self.plan['content_hash'],
            'mission_version_ref':'mission-version:test','mission_version_hash':'a'*64,
            'authorization':auth,'actor_ref':'automation:test','query_hash':refused['query_hash']}
        ticket={**common,'id':ref,'status':'failed','started_at':start,'completed_at':done,'exit_code':1,'actor_ref':'automation:test',
            'as_of':self.h.clock().date().isoformat(),'governance_ref':'governance:test','governance_hash':'f'*64,
            'plan_ref':self.plan['id'],'plan_hash':self.plan['content_hash'],
            'mission_version_ref':dispatch['mission_version_ref'],'mission_version_hash':'a'*64}
        summary={**common,'status':'failed','plan_ref':self.plan['id'],'plan_hash':self.plan['content_hash'],
            'as_of':ticket['as_of'],'governance_ref':'governance:test','governance_hash':'f'*64,
            'search':None,'discovery_ref':None,'provider_calls':0,'authorization':auth,
            'parameters':refused['parameters'],'query_hash':refused['query_hash'],
            'failure_reason':'unexpected ConnectorQuotaExceeded: connector calls quota exceeded'}
        _write(directory/'ticket.json',ticket);_write(directory/'summary.json',summary)
        import re
        launcher=SimpleNamespace(state_dir=self.fx.state,_ticket_re=re.compile(r'alphaengine-discovery:[a-f0-9]{24}'),
            _ticket_path=lambda _:directory/'ticket.json')
        coordinator=SimpleNamespace(plan=self.plan,source_ref='source:alphaengine',search_launcher=launcher,
            store=self.h.core,_core_record=MissionSourceDiscoveryCoordinator._core_record,
            missions=SimpleNamespace(authorize_source_discovery=lambda **_:auth))
        self.assertFalse(quota_increase_recovered(coordinator,dispatch))
        self.authorize()
        self.assertTrue(quota_increase_recovered(coordinator,dispatch))
        # An earlier human save must not retroactively rewrite which policy
        # was registered at the old admitted barrier.
        self.h.clock.advance(seconds=1)
        self.h.search.quota_authority=load_authority(self.fx.state);self.h.search._authorities=None
        self.h.search.ensure_governed_authorities()
        self.assertTrue(quota_increase_recovered(coordinator,dispatch))
        for field,val in [('provider_calls',1),('query_hash','b'*64),
                          ('failure_reason','unexpected TIMEOUT: unknown')]:
            _write(directory/'summary.json',{**summary,field:val})
            self.assertFalse(quota_increase_recovered(coordinator,dispatch))
        _write(directory/'summary.json',summary)
        self.assertFalse(quota_increase_recovered(coordinator,{**dispatch,'mission_version_hash':'b'*64}))
