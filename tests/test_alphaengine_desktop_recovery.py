"""Only actual same-capability success supersedes the old module-page hold."""
import copy
import json
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from dalton_core.alphaengine_core_search import FakeSearchHandle,search_spec_hash
from dalton_core.live_mcp_connector import HostToolInvocationResult
from dalton_core.mission_source_discovery import build_discovery_parameters
from dalton_core.alphaengine_desktop_recovery import desktop_module_recovered,_terminal
from dalton_core.provider_failure import journal_failure,failure_reason_text
from dalton_core.store import canonical_json,content_hash
from tests import test_mission_source_discovery as fixtures


class NoModuleHandle:
    def invoke(self,tool_name,arguments,**kwargs):
        result={'isError':True,'content':[{'type':'text','text':'E_DESKTOP_NO_MODULE_PAGE: Desktop is running but no module page is open'}]}
        raw=canonical_json({'jsonrpc':'2.0','id':'module-error','result':result}).encode()
        return HostToolInvocationResult(request_id='module-error',raw_response=raw,result=result)


class DesktopRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.new_fixture()

    def new_fixture(self):
        self.f=fixtures.CoordinatorTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.mission=self.f.mission_v2(self.f.create_mission());self.c=self.f.coordinator
        self.launcher=self.f.search_launcher
        self.launcher._ticket_re=re.compile(r'alphaengine-discovery:[0-9a-f]{24}\Z')
        self.launcher._ticket_path=lambda ref:Path(self.f.temp.name)/'proof-tickets'/ref.split(':')[1]/'ticket.json'
        self.launcher.spool_dir=Path(self.f.temp.name)/'spool'
        self.number=100

    def dispatch(self,company=fixtures.ACN,*,succeeded=False):
        auth=self.f.missions.authorize_source_discovery(company_ref=company,
            source_ref='source:alphaengine',requested_by=fixtures.AUTOMATION)
        parameters=build_discovery_parameters(self.f.plan,spec_ref='earnings-call-transcripts',company_ref=company,as_of=self.f.clock().date())
        self.f.h.search.credentials._handle_resolver=lambda grant:FakeSearchHandle([]) if succeeded else NoModuleHandle()
        receipt=self.f.h.search.search(self.f.h.search.build_request(parameters))
        self.number+=1;ref=f'alphaengine-discovery:{self.number:024x}'
        dispatch=self.f.missions.record_discovery_dispatch(authorization=auth,
            discovery_plan_ref=self.f.plan['id'],discovery_plan_hash=self.f.plan['content_hash'],
            spec_ref='earnings-call-transcripts',query_hash=search_spec_hash(parameters),ticket_ref=ref)
        discovery=None;failure=None
        if succeeded:
            discovery=self.f.missions.record_source_discovery(authorization=auth,
                discovery_plan_ref=self.f.plan['id'],discovery_plan_hash=self.f.plan['content_hash'],
                spec_ref='earnings-call-transcripts',query_hash=search_spec_hash(parameters),parameters=parameters,
                connector_invocation_ref=receipt['connector_invocation_ref'],connector_invocation_hash=receipt['connector_invocation_hash'],
                source_envelope_ref=receipt['source_envelope_ref'],source_envelope_hash=receipt['source_envelope_hash'],
                document_refs=receipt['document_refs'],in_authority_document_refs=[])['id']
        else:failure=failure_reason_text('search outcome retryable',journal_failure(self.f.h.core.connection,receipt['connector_invocation_ref']))
        summary={'status':'succeeded' if succeeded else 'failed','company_ref':company,'spec_ref':'earnings-call-transcripts',
            'source_ref':'source:alphaengine','requested_by':auth['requested_by'],'plan_ref':self.f.plan['id'],'plan_hash':self.f.plan['content_hash'],
            'governance_ref':'governance:test','governance_hash':'a'*64,'as_of':self.f.clock().date().isoformat(),
            'authorization':auth,'parameters':parameters,'query_hash':search_spec_hash(parameters),'search':receipt,
            'discovery_ref':discovery,'failure_reason':failure}
        ticket={k:summary[k] for k in ('company_ref','spec_ref','requested_by','as_of','plan_ref','plan_hash','governance_ref','governance_hash')}
        ticket.update(id=ref,status=summary['status'],exit_code=0 if succeeded else 1,completed_at=self.f.clock().isoformat(),
            actor_ref=auth['actor_ref'],mission_version_ref=auth['mission_version_ref'],mission_version_hash=auth['mission_version_hash'])
        path=self.launcher._ticket_path(ref);path.parent.mkdir(parents=True);path.write_text(json.dumps(ticket));path.with_name('summary.json').write_text(json.dumps(summary))
        self.launcher.tickets[ref]={**ticket,'summary':summary}
        row=self.f.missions.settle_discovery_dispatch(dispatch['dispatch_id'],status=summary['status'],**({}if succeeded else {'reason':failure}))
        self.f.clock.advance(minutes=1)
        return row,path,receipt

    def test_two_companies_later_other_company_strong_success_reopens_only_normal_query(self):
        for index,(company,other) in enumerate(((fixtures.ACN,fixtures.CTSH),(fixtures.CTSH,fixtures.ACN))):
            if index:self.new_fixture()
            with self.subTest(company=company):
                failed,path,receipt=self.dispatch(company)
                self.assertFalse(desktop_module_recovered(self.c,failed))
                self.dispatch(other,succeeded=True)
                con=self.f.h.core.connection
                tables=('connector_quota_reservations','connector_cost_entries','connector_quota_settlements')
                old={t:[tuple(r)for r in con.execute('SELECT * FROM '+t)]for t in tables}
                oldfiles=path.read_bytes(),path.with_name('summary.json').read_bytes()
                self.assertTrue(desktop_module_recovered(self.c,failed))
                self.assertIsNone(self.c._cadence_block(self.mission['id'],company,self.f.plan['specs'][0]))
                self.assertEqual({t:[tuple(r)for r in con.execute('SELECT * FROM '+t)]for t in tables},old)
                self.assertEqual((path.read_bytes(),path.with_name('summary.json').read_bytes()),oldfiles)
                self.f.h.search.credentials._handle_resolver=lambda grant:FakeSearchHandle([])
                with patch.object(self.c,'_continuation_page',return_value=None):result=self.c.launch_discovery()
                self.assertEqual(result['status'],'launched')
                self.assertIsNone(self.launcher.starts[-1]['cursor'])
                self.c.settle_dispatches()
                # A later provider failure cannot reuse a success predating it.
                later,_,_=self.dispatch(company)
                self.assertFalse(desktop_module_recovered(self.c,later))
                self.assertIn('retry interval',self.c._cadence_block(self.mission['id'],company,self.f.plan['specs'][0]))

    def test_auth_ready_and_success_before_failure_are_not_recovery_proof(self):
        self.dispatch(fixtures.CTSH,succeeded=True)
        failed,_,_=self.dispatch()
        self.c.health=type('Health',(),{'record':lambda *a:{'state':'ready','recovered_at':self.f.clock().isoformat()}})()
        self.assertFalse(desktop_module_recovered(self.c,failed))
        self.assertIn('retry interval',self.c._cadence_block(self.mission['id'],fixtures.ACN,self.f.plan['specs'][0]))

    def test_raw_bytes_missing_or_changed_and_source_envelope_tamper_refuse(self):
        failed,_,_=self.dispatch();success,_,receipt=self.dispatch(fixtures.CTSH,succeeded=True)
        self.assertTrue(desktop_module_recovered(self.c,failed))
        proof=_terminal(self.c,success,succeeded=True)
        source=self.c._core_record(self.f.h.core.connection,'connector_source_envelopes','source_envelope_id',receipt['source_envelope_ref'])
        rawpath=self.f.h.spool._objects/source['raw_response_hash'][:2]/source['raw_response_hash']
        raw=rawpath.read_bytes();rawpath.write_bytes(raw+b' ')
        self.assertFalse(desktop_module_recovered(self.c,failed));rawpath.write_bytes(raw)
        rawpath.rename(rawpath.with_suffix('.missing'))
        self.assertFalse(desktop_module_recovered(self.c,failed));rawpath.with_suffix('.missing').rename(rawpath)
        self.assertTrue(desktop_module_recovered(self.c,failed))
        # Even a self-rehashed normalized source cannot override its response's hash.
        con=self.f.h.core.connection
        con.execute('DROP TRIGGER connector_source_envelopes_no_update')
        source['source_record_refs']=['alphaengine-doc:foreign'];source['content_hash']=content_hash({k:v for k,v in source.items()if k!='content_hash'})
        con.execute('UPDATE connector_source_envelopes SET record_json=?,content_hash=? WHERE source_envelope_id=?',
            (canonical_json(source),source['content_hash'],source['id']));con.commit()
        self.assertFalse(desktop_module_recovered(self.c,failed))

    def test_journal_incomplete_and_unrelated_typed_error_do_not_release(self):
        failed,path,_=self.dispatch();self.dispatch(fixtures.CTSH,succeeded=True)
        self.assertTrue(desktop_module_recovered(self.c,failed))
        changed={**failed,'failure_reason':'search outcome retryable [provider_error] source_error: HTTP_500'}
        self.assertFalse(desktop_module_recovered(self.c,changed))
        con=self.f.h.core.connection;con.execute('DROP TRIGGER runner_attempt_journal_no_delete')
        con.execute("DELETE FROM runner_attempt_journal_events WHERE state='responded'")
        self.assertFalse(desktop_module_recovered(self.c,failed))

    def test_profile_environment_or_descriptor_mismatch_cannot_release(self):
        failed,_,_=self.dispatch();succeeded,_,_=self.dispatch(fixtures.CTSH,succeeded=True)
        import dalton_core.alphaengine_desktop_recovery as recovery
        good_failure=_terminal(self.c,failed);good_success=_terminal(self.c,succeeded,succeeded=True)
        for group,key,value in (('invocation','connector_profile_hash','f'*64),
                                ('invocation','descriptor_revision_ref','capability:foreign'),
                                ('request','runner_environment_hash','f'*64),
                                ('request','principal_ref','principal:foreign')):
            bad={**good_success,group:{**good_success[group],key:value}}
            with self.subTest(field=key),patch.object(recovery,'_terminal',side_effect=[good_failure,bad]):
                self.assertFalse(desktop_module_recovered(self.c,failed))

    def test_normal_source_permission_plan_stop_budget_and_bounded_deadline_remain(self):
        failed,_,_=self.dispatch();self.dispatch(fixtures.CTSH,succeeded=True)
        self.assertTrue(desktop_module_recovered(self.c,failed))
        with patch.object(self.f.missions,'authorize_source_discovery',side_effect=fixtures.CoverageMissionConflict('revoked')):
            self.assertFalse(desktop_module_recovered(self.c,failed))
        self.c.owner_call_cap=0
        self.assertEqual(self.c.launch_discovery()['status'],'budget_exhausted');self.assertEqual(self.launcher.starts,[])
        with patch.object(self.c,'_plan_decision',return_value=('plan says stop',None)):
            self.assertEqual(self.c._spec_block(self.mission,fixtures.ACN,self.f.plan['specs'][0]),'plan says stop')
        with patch('dalton_core.alphaengine_desktop_recovery.time.monotonic',side_effect=[100,104]):
            self.assertFalse(desktop_module_recovered(self.c,failed))
