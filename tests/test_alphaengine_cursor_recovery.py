"""Exact expired cursor refusal reopens a governed query, not an old call."""
import copy
import json
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dalton_core.alphaengine_core_search import FakeSearchHandle, search_spec_hash
from dalton_core.live_mcp_connector import HostToolInvocationResult
from dalton_core.mission_source_discovery import build_discovery_parameters, build_discovery_plan, MissionSourceDiscoveryCoordinator
from dalton_core.alphaengine_cursor_recovery import expired_cursor_restart_available
from dalton_core.provider_failure import journal_failure, failure_reason_text
from dalton_core.store import canonical_json, content_hash
from tests import test_mission_source_discovery as fixture


class RefusedHandle:
    def invoke(self, tool_name, arguments, **kwargs):
        result = {'isError': True, 'content': [{'type': 'text',
            'text': 'E_CURSOR_EXPIRED: 搜索游标不存在或已超过 15 分钟，请重新检索'}]}
        raw = canonical_json({'jsonrpc': '2.0', 'id': 'provider:expired', 'result': result}).encode()
        return HostToolInvocationResult(request_id='provider:expired', raw_response=raw, result=result)


class CursorRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.CoordinatorTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.mission = self.f.mission_v2(self.f.create_mission())
        self.c = self.f.coordinator
        self.launcher = self.f.search_launcher
        self.launcher._ticket_re = re.compile(r'alphaengine-discovery:[0-9a-f]{24}\Z')
        self.launcher._ticket_path = lambda ref: Path(self.f.temp.name) / 'proof-tickets' / ref.split(':')[1] / 'ticket.json'

    def failed(self, company=fixture.ACN, number=1, *, cursor='ae1:expired'):
        auth = self.f.missions.authorize_source_discovery(company_ref=company,
            source_ref='source:alphaengine', requested_by=fixture.AUTOMATION)
        parameters = build_discovery_parameters(self.f.plan, spec_ref='earnings-call-transcripts',
            company_ref=company, as_of=self.f.clock().date(), cursor=cursor)
        # The adapter receives the host handle through the governed executor.
        self.f.h.search.credentials._handle_resolver = lambda grant: RefusedHandle()
        receipt = self.f.h.search.search(self.f.h.search.build_request(parameters))
        ref = f'alphaengine-discovery:{number:024x}'
        dispatch = self.f.missions.record_discovery_dispatch(authorization=auth,
            discovery_plan_ref=self.f.plan['id'], discovery_plan_hash=self.f.plan['content_hash'],
            spec_ref='earnings-call-transcripts', query_hash=search_spec_hash(parameters), ticket_ref=ref)
        detail = journal_failure(self.f.h.core.connection, receipt['connector_invocation_ref'])
        summary = {'status': 'failed', 'company_ref': company, 'spec_ref': 'earnings-call-transcripts',
            'source_ref': 'source:alphaengine', 'requested_by': auth['requested_by'],
            'plan_ref': self.f.plan['id'], 'plan_hash': self.f.plan['content_hash'],
            'governance_ref': 'governance:test', 'governance_hash': 'a'*64,
            'as_of': self.f.clock().date().isoformat(), 'authorization': auth,
            'parameters': parameters, 'query_hash': search_spec_hash(parameters),
            'search': receipt, 'discovery_ref': None,
            'failure_reason': failure_reason_text('search outcome retryable', detail)}
        ticket = {k: summary[k] for k in ('company_ref','spec_ref','requested_by','as_of',
            'plan_ref','plan_hash','governance_ref','governance_hash')}
        ticket.update(id=ref, status='failed', exit_code=1, completed_at=self.f.clock().isoformat(),
            actor_ref=auth['actor_ref'], mission_version_ref=auth['mission_version_ref'],
            mission_version_hash=auth['mission_version_hash'])
        path = self.launcher._ticket_path(ref);path.parent.mkdir(parents=True)
        path.write_text(json.dumps(ticket));path.with_name('summary.json').write_text(json.dumps(summary))
        self.launcher.tickets[ref] = {**ticket, 'summary': summary}
        settled = self.f.missions.settle_discovery_dispatch(dispatch['dispatch_id'],
            status='failed', reason=summary['failure_reason'])
        self.f.clock.advance(minutes=1)
        return settled, path, receipt

    def test_two_companies_exact_runner_proof_and_normal_fresh_query(self):
        for number, company in enumerate((fixture.ACN, fixture.CTSH), 1):
            with self.subTest(company=company):
                dispatch, path, receipt = self.failed(company, number)
                self.assertTrue(expired_cursor_restart_available(self.c, dispatch))
                before = path.read_bytes(), path.with_name('summary.json').read_bytes()
                spec = self.f.plan['specs'][0]
                self.assertIsNone(self.c._cadence_block(self.mission['id'], company, spec))
                self.assertEqual(before, (path.read_bytes(), path.with_name('summary.json').read_bytes()))
        self.f.h.search.credentials._handle_resolver = lambda grant: FakeSearchHandle([])
        with patch.object(self.c, '_continuation_page', return_value=None):
            result = self.c.launch_discovery()
        self.assertEqual(result['status'], 'launched')
        self.assertIsNone(self.launcher.starts[-1]['cursor'])
        self.c.settle_dispatches()
        self.assertIn('rediscovered', self.c._cadence_block(self.mission['id'], fixture.ACN, self.f.plan['specs'][0]))

    def test_cursor_free_failure_never_gets_cadence_exception(self):
        dispatch, _, _ = self.failed(cursor=None)
        self.assertFalse(expired_cursor_restart_available(self.c, dispatch))
        self.assertIn('retry interval', self.c._cadence_block(self.mission['id'], fixture.ACN, self.f.plan['specs'][0]))

    def test_summary_hash_or_company_drift_and_incomplete_journal_refuse(self):
        dispatch, path, receipt = self.failed()
        original = path.with_name('summary.json').read_bytes()
        for field, value in [('company_ref', fixture.CTSH), ('query_hash', 'b'*64),
                             ('authorization', {}), ('search', {})]:
            changed = json.loads(original);changed[field] = value
            path.with_name('summary.json').write_text(json.dumps(changed))
            self.assertFalse(expired_cursor_restart_available(self.c, dispatch))
        path.with_name('summary.json').write_bytes(original)
        con = self.f.h.core.connection
        con.execute("DROP TRIGGER runner_attempt_journal_no_delete")
        con.execute("DELETE FROM runner_attempt_journal_events WHERE state='responded'")
        self.assertFalse(expired_cursor_restart_available(self.c, dispatch))

    def test_rehashed_observation_and_wrong_failure_keep_retry_interval(self):
        dispatch, path, receipt = self.failed()
        con = self.f.h.core.connection
        con.execute("DROP TRIGGER runner_attempt_journal_no_update")
        event = self.f.h.journal.history('connector-runner-request:alphaengine-search:' + receipt['request_hash'][:20])[-2]
        self.assertEqual(event['state'], 'observed')
        event['payload']['error']['message'] = 'E_CURSOR_UNKNOWN: ambiguous transport'
        body = {k: v for k, v in event.items() if k not in {'id','content_hash'}}
        digest = content_hash(body)
        ref = 'runner-journal-event:' + content_hash({'runner_request_ref':event['runner_request_ref'],
            'request_ordinal':event['request_ordinal'], 'content_hash':digest})
        con.execute('UPDATE runner_attempt_journal_events SET payload_json=?,content_hash=?,event_id=? WHERE event_id=?',
            (canonical_json(event['payload']), digest, ref, event['id']))
        self.assertFalse(expired_cursor_restart_available(self.c, dispatch))

    def test_open_dispatch_and_source_permission_still_block(self):
        dispatch, path, receipt = self.failed()
        open_dispatch = dict(dispatch, status='launched')
        self.assertFalse(expired_cursor_restart_available(self.c, open_dispatch))
        with patch.object(self.f.missions, 'authorize_source_discovery', side_effect=fixture.CoverageMissionConflict('revoked')):
            self.assertFalse(expired_cursor_restart_available(self.c, dispatch))


    def test_normal_budget_and_plan_stop_gates_still_apply(self):
        dispatch, _, _ = self.failed()
        self.assertTrue(expired_cursor_restart_available(self.c, dispatch))
        self.c.owner_call_cap = 1
        result = self.c.launch_discovery()
        self.assertEqual(result['status'], 'budget_exhausted')
        self.assertEqual(self.launcher.starts, [])
        with patch.object(self.c, '_plan_decision', return_value=('plan says stop', None)):
            self.assertEqual(self.c._spec_block(self.mission, fixture.ACN, self.f.plan['specs'][0]), 'plan says stop')

    def test_stale_cursor_after_selection_resets_exact_page_variant(self):
        class PageHandle(FakeSearchHandle):
            def invoke(self, *args, **kwargs):
                original = super().invoke(*args, **kwargs)
                payload = json.loads(original.result['content'][0]['text'])
                payload.update(cursor='ae1:next-page', has_more=True)
                result = {'content':[{'type':'text','text':canonical_json(payload)}]}
                raw = canonical_json({'jsonrpc':'2.0','id':original.request_id,'result':result}).encode()
                return HostToolInvocationResult(request_id=original.request_id, raw_response=raw, result=result)
        handle = PageHandle([{'doc_id': fixture.KNOWN_DOC.split(':')[1]}])
        self.f.h.search.credentials._handle_resolver = lambda grant: handle
        plan = build_discovery_plan(plan_id='discovery-plan:cursor-lifetime:test',
            created_at=self.f.clock().isoformat(), mission_ref=self.f.plan['mission_ref'],
            source_ref='source:alphaengine', max_calls_24h=30,
            companies={fixture.ACN:{'name':'Accenture','ticker':'ACN','aliases':['Accenture']}},
            specs=[{'spec_ref':'earnings-call-transcripts','document_type':'meeting_minutes',
                'query_variants':[{'query_template':'{ticker} earnings call transcript','filters':{}}],
                'lookback_days':400,'rediscovery_interval_days':7,'retry_interval_days':1}])
        launcher = fixture.FakeSearchLauncher(self.f.h, self.f.missions, plan)
        selection = SimpleNamespace(consumed=[], quarantine=SimpleNamespace(active=lambda **kw: []))
        selection.currently_consumed = lambda **kw: list(selection.consumed)
        c = MissionSourceDiscoveryCoordinator(store=self.f.h.core, missions=self.f.missions,
            plan=plan, search_launcher=launcher, acquisition_launcher=None,
            clock=self.f.clock, selection_launcher=selection)
        self.assertEqual(c.launch_discovery()['status'], 'launched')
        c.settle_dispatches()
        discovery = self.f.missions.source_discoveries(self.mission['id'], company_ref=fixture.ACN,
            spec_ref='earnings-call-transcripts')[0]
        self.assertIsNone(c._continuation_page(self.mission['id'],fixture.ACN,'earnings-call-transcripts'))
        self.f.clock.advance(minutes=14)
        selection.consumed = [discovery['id']]
        self.assertEqual(c._continuation_page(self.mission['id'],fixture.ACN,'earnings-call-transcripts')['cursor'], 'ae1:next-page')
        def slow_selection(**kwargs):
            self.f.clock.advance(minutes=1)
            return list(selection.consumed)
        selection.currently_consumed = slow_selection
        continued = c._continuation_page(self.mission['id'],fixture.ACN,'earnings-call-transcripts')
        self.assertIsNone(continued['cursor'])
        self.assertEqual(continued['variant_index'], 0)
        self.assertEqual(continued['as_of'], discovery['parameters']['filters']['date_to'])
        selection.currently_consumed = lambda **kw: list(selection.consumed)
        selection.consumed = []
        self.assertIsNone(c._continuation_page(self.mission['id'],fixture.ACN,'earnings-call-transcripts'))


if __name__ == '__main__': unittest.main()
