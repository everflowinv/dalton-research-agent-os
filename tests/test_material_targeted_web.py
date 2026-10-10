from __future__ import annotations
import copy
import json
import sqlite3
import unittest
from datetime import date
from unittest import mock
from dalton_core import material_acquisition as ma, material_requests as mr
from dalton_core.material_targeted_web import targeted_web_parameters, TargetedWebError
from dalton_core.mission_source_discovery import WebSearchLauncher, DiscoveryLaunchRejected, build_discovery_parameters
from dalton_core.public_web_search_cli import run_discovery
from dalton_core.public_web_core_search import FakeWebSearchHandle, WebSearchConnectorGovernance
from tests.test_mission_web_search_discovery import WebSearchChildTests, ACN, OWNER

class TargetedWebTests(unittest.TestCase):
    setUp=WebSearchChildTests.setUp
    authorization=WebSearchChildTests.authorization
    def queued(self,question="What evidence explains synthetic customer pricing changes?"):
        request=mr.open_request(self.state,requester={"lane":"company_dossier","section":"catalysts"},
            subject=ACN,need={"kind":"answer","question":question},source_priority=["web"],
            now="2026-09-06T00:00:00+00:00")
        phrases=ma.build_queries(request,ma.identity_for(self.state,ACN),now="2026-09-06T00:00:00+00:00")
        query=ma.enqueue_targeted_query(self.state,request_id=request["id"],source="web",company_ref=ACN,
            spec_ref="web",phrasings=phrases,now="2026-09-06T00:00:00+00:00")
        return request,query
    def params(self,query,binding=None,**kwargs):
        return targeted_web_parameters(self.state,plan=self.plan,company_ref=ACN,
            spec_ref="management-changes",as_of=date(2026,9,6),
            mission_version_ref=self.mission["id"],mission_version_hash=self.mission["content_hash"],
            **({"binding":binding} if binding is not None else {"query_ref":query["query_id"]}),**kwargs)
    def launcher(self):
        return WebSearchLauncher(state_dir=self.state,governance_path=self.governance_path,
            plan_path=self.plan_path,mode_args=("--fake-citations-file",str(self.citations_path)))
    def test_exact_question_reaches_real_child_invocation_and_formal_discovery(self):
        request,query=self.queued();parameters,binding=self.params(query)
        old=build_discovery_parameters(self.plan,spec_ref="management-changes",company_ref=ACN,as_of=date(2026,9,6))
        self.assertNotEqual(parameters["query"],old["query"])
        self.assertEqual(parameters["query"],query["phrasings"][0])
        self.assertEqual({k:parameters[k] for k in ("date_after","date_before")},
                         {k:old[k] for k in ("date_after","date_before")})
        launcher=self.launcher();ticket=launcher.start(authorization=self.authorization(),
            spec_ref="management-changes",as_of=date(2026,9,6),targeted_binding=binding)
        self.assertEqual(launcher.wait(timeout=30),0)
        result=launcher.status(ticket["id"]);summary=result["summary"]
        self.assertEqual(summary["parameters"],parameters)
        self.assertEqual(summary["targeted_binding"],binding)
        self.assertEqual(result["targeted_binding"],binding)
        self.assertEqual((summary["status"],summary["provider_calls"]),("succeeded",1))
        connection=sqlite3.connect(self.state/'core.sqlite');connection.row_factory=sqlite3.Row
        try:
            row=connection.execute("SELECT record_json FROM coverage_mission_source_discoveries WHERE record_id=?",
                (summary["discovery_ref"],)).fetchone()
            record=json.loads(row[0]);self.assertEqual(record["parameters"],parameters)
            self.assertEqual(record["query_hash"],summary["query_hash"])
        finally:connection.close()
    def test_changed_query_or_request_binding_refuses_before_spawn(self):
        request,query=self.queued();_,binding=self.params(query)
        for key in ("query_hash","request_hash","plan_hash","mission_version_hash","company_ref"):
            bad={**binding,key:"0"*64}
            with mock.patch('subprocess.Popen') as spawn, self.assertRaises(DiscoveryLaunchRejected):
                self.launcher().start(authorization=self.authorization(),spec_ref="management-changes",
                    as_of=date(2026,9,6),targeted_binding=bad)
            spawn.assert_not_called()
    def test_arbitrary_queued_phrasings_and_sql_projection_drift_refuse(self):
        request,query=self.queued()
        path=mr.ledger_path(self.state)
        connection=sqlite3.connect(path)
        connection.execute('DROP TRIGGER material_targeted_queries_no_update')
        raw=dict(query);raw.pop('query_id');raw.pop('write_status');raw['phrasings']=['unrelated free query']
        connection.execute('UPDATE material_targeted_queries SET record_json=? WHERE query_id=?',
            (ma._canonical(raw),query['query_id']));connection.commit();connection.close()
        with self.assertRaisesRegex(TargetedWebError,'phrasings'):self.params(query)
    def test_closed_request_is_zero_call_in_child_and_can_still_be_verified_at_settlement(self):
        request,query=self.queued();_,binding=self.params(query)
        mr.close(self.state,request['id'],status='found',reason='synthetic local result')
        handle=FakeWebSearchHandle([])
        result=run_discovery(state_dir=self.state,governance=WebSearchConnectorGovernance.load(self.governance_path),
            plan=self.plan,company_ref=ACN,spec_ref='management-changes',requested_by=OWNER,
            mission_version_ref=self.mission['id'],mission_version_hash=self.mission['content_hash'],
            as_of=date(2026,9,6),handle=handle,transport='fake',summary_dir=self.root/'out',targeted_binding=binding)
        self.assertEqual((result['status'],result['provider_calls']),("failed",0))
        self.assertIn('no longer active',result['failure_reason'])
        self.assertEqual(self.params(query,binding,allow_closed=True)[1],binding)
    def test_corrupt_request_record_and_wrong_company_or_spec_do_not_fallback(self):
        request,query=self.queued();_,binding=self.params(query)
        bad={**binding,'spec_ref':'not-authorized'}
        with self.assertRaises(TargetedWebError):self.params(query,bad)
        connection=sqlite3.connect(mr.ledger_path(self.state));connection.execute('DROP TRIGGER material_requests_no_update')
        connection.execute('UPDATE material_requests SET subject_ref=? WHERE request_id=?',('company:other',request['id']))
        connection.commit();connection.close()
        with self.assertRaisesRegex(TargetedWebError,'identity/hash'):self.params(query)
    def test_real_coordinator_queue_child_and_settlement_keep_exact_binding(self):
        from datetime import datetime,timezone
        from dalton_core.store import DaltonStore
        from dalton_core.coverage_mission import CoverageMissionAuthority
        from dalton_core.mission_source_discovery import MissionSourceDiscoveryCoordinator
        from tests.test_mission_web_search_discovery import mission_with_web_status
        core=DaltonStore(str(self.state/'core.sqlite'));self.addCleanup(core.close)
        from dalton_core.connector import ConnectorStore
        ConnectorStore(core)
        missions=CoverageMissionAuthority(core)
        params=mission_with_web_status(self.method,status='connected',grant=True,version=3,prior=self.mission)
        ref=params.pop('mission_ref');self.mission=missions.create_mission(ref,**params)
        request,query=self.queued();launcher=self.launcher()
        coordinator=MissionSourceDiscoveryCoordinator(store=core,missions=missions,plan=self.plan,
            search_launcher=launcher,acquisition_launcher=None,clock=lambda:datetime(2026,9,6,tzinfo=timezone.utc))
        launched=coordinator.launch_targeted_discovery()
        self.assertEqual(launched['status'],'launched')
        self.assertEqual(launcher.wait(timeout=30),0)
        ticket=launcher.status(launched['ticket_ref'])
        self.assertEqual(ticket['summary']['parameters']['query'],query['phrasings'][0])
        dispatched=missions.open_discovery_dispatches(source_ref='source:web-search')
        self.assertEqual(dispatched[0]['query_hash'],ticket['summary']['query_hash'])
        settled=coordinator.settle_dispatches()
        self.assertEqual(settled[0]['status'],'succeeded')
        self.assertIsNone(coordinator.launch_targeted_discovery())
        self.assertEqual(ma.targeted_query_status(self.state,query['query_id'])['status'],'launched')
    def test_arbitrary_binding_field_is_not_a_query_override(self):
        _,query=self.queued();_,binding=self.params(query)
        with self.assertRaises(TargetedWebError):self.params(query,{**binding,'query':'unrelated override'})
        self.assertFalse((self.state/'connector-spool').exists())
    def test_budget_exhaustion_does_not_dequeue_or_spawn(self):
        from types import SimpleNamespace
        from datetime import datetime,timezone
        from dalton_core.mission_source_discovery import MissionSourceDiscoveryCoordinator
        request,query=self.queued();launcher=mock.Mock(state_dir=self.state);launcher.running.return_value=False
        missions=mock.Mock();missions.active_mission.return_value=self.mission
        missions.authorize_source_discovery.return_value=self.authorization()
        fake=SimpleNamespace(search_launcher=launcher,missions=missions,source_ref='source:web-search',
            plan=self.plan,clock=lambda:datetime(2026,9,6,tzinfo=timezone.utc),_health_hold=lambda:None,
            _budget=lambda cap:{'remaining':0},_missing_periods=lambda *args:[])
        self.assertIsNone(MissionSourceDiscoveryCoordinator.launch_targeted_discovery(fake))
        launcher.start.assert_not_called()
        self.assertEqual(len(ma.pending_targeted_queries(self.state,'source:web-search')),1)
    def test_sql_query_column_drift_and_missing_ledger_fail_closed(self):
        _,query=self.queued()
        connection=sqlite3.connect(mr.ledger_path(self.state));connection.execute('DROP TRIGGER material_targeted_queries_no_update')
        connection.execute('UPDATE material_targeted_queries SET company_ref=? WHERE query_id=?',('company:other',query['query_id']))
        connection.commit();connection.close()
        with self.assertRaisesRegex(TargetedWebError,'identity'):self.params(query)
        mr.ledger_path(self.state).unlink()
        with self.assertRaisesRegex(TargetedWebError,'missing'):self.params(query)

    def test_settlement_rejects_child_query_drift_without_replay(self):
        from types import SimpleNamespace
        from dalton_core.mission_source_discovery import MissionSourceDiscoveryCoordinator
        _,query=self.queued();parameters,binding=self.params(query)
        dispatch={'dispatch_id':'synthetic:dispatch','ticket_ref':'synthetic:ticket',
            'company_ref':ACN,'spec_ref':'management-changes','query_hash':'0'*64,
            'mission_version_ref':self.mission['id'],'mission_version_hash':self.mission['content_hash']}
        ticket={'status':'succeeded','as_of':'2026-09-06','targeted_binding':binding,
            'summary':{'targeted_binding':binding,'parameters':{**parameters,'query':'unrelated'},
                       'query_hash':'0'*64,'discovery_ref':'synthetic:discovery'}}
        launcher=mock.Mock(state_dir=self.state);launcher.status.return_value=ticket
        missions=mock.Mock();missions.open_discovery_dispatches.return_value=[dispatch]
        missions.settle_discovery_dispatch.return_value={'status':'failed','failure_reason':'binding drift'}
        fake=SimpleNamespace(missions=missions,search_launcher=launcher,source_ref='source:web-search',plan=self.plan)
        result=MissionSourceDiscoveryCoordinator.settle_dispatches(fake)
        self.assertEqual(result[0]['status'],'failed')
        self.assertEqual(missions.settle_discovery_dispatch.call_args.kwargs['status'],'failed')
        launcher.start.assert_not_called()

# Reuse only fixture setup helpers, not discovery of the imported test class.
del WebSearchChildTests
