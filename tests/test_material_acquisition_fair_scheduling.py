"""Persistent bounded scheduling over real requests, without connector calls."""
from contextlib import closing, contextmanager
import sqlite3
import unittest
from unittest import mock
from dalton_core import material_acquisition as ma, material_requests as mr
from tests import test_material_acquisition as fixture


class FairMaterialSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.fx=fixture.Fixture();self.fx.setUp();self.addCleanup(self.fx.doCleanups)
        self.state=self.fx.state

    def requests(self, count, *, numeric=False):
        return [self.fx.request(need={'kind':'answer','question':f'Exact research question {i}'},
                lane='initial_screen' if i<4 else 'research_planner',
                acceptance={'numeric_contract_ref':'directed-document-numeric:0.1'} if numeric and i<4 else None,
                source_priority=['indexed_corpus']) for i in range(count)]

    def test_waiting_and_budget_blocked_prefix_does_not_starve_later_requests(self):
        requests=self.requests(9);seen=[]
        def serve(state,ref,**kwargs):
            seen.append(ref)
            return {'request_id':ref,'outcome':'budget_blocked' if ref==requests[0]['id'] else 'waiting',
                    'budget':{'scope':'request'}}
        with mock.patch.object(ma,'serve',side_effect=serve):
            summaries=[ma.tick(self.state,now=fixture.NOW,convert=False)for _ in range(3)]
        self.assertEqual(set(seen),{x['id']for x in requests})
        self.assertTrue(all(x['served']<=4 for x in summaries))
        self.assertEqual(seen[:4],[x['id']for x in sorted(requests,key=lambda x:ma.priority_key(x,{}))[:4]])
        for req in requests:self.assertEqual(mr.request(self.state,req['id'])['attempt_count'],0)
        self.assertEqual(ma.request_spend(self.state,requests[0]['id']),{'calls':0,'cost_micros':0})

    def test_real_numeric_waits_leave_attempts_untouched_and_next_free_request_is_served(self):
        requests=self.requests(5,numeric=True)
        rung=fixture.FakeRung('indexed_corpus',['empty'],calls=0);rung.costed=False
        first=ma.tick(self.state,now=fixture.NOW,convert=False,rungs={'indexed_corpus':rung})
        self.assertEqual(first['waiting'],4);self.assertEqual(rung.runs,0)
        second=ma.tick(self.state,now=fixture.NOW,convert=False,rungs={'indexed_corpus':rung})
        self.assertIn(requests[4]['id'],[x['request_id']for x in second['results']])
        self.assertEqual(rung.runs,1)
        for req in requests[:4]:
            view=mr.request(self.state,req['id']);self.assertEqual(view['attempt_count'],0)
            self.assertEqual(view['status'],'open');self.assertEqual(ma.request_spend(self.state,req['id']),{'calls':0,'cost_micros':0})

    def test_closed_anchor_and_new_priority_request_do_not_reset_rotation(self):
        requests=self.requests(8);seen=[]
        with mock.patch.object(ma,'serve',side_effect=lambda state,ref,**kw:seen.append(ref) or {'request_id':ref,'outcome':'waiting'}):
            ma.tick(self.state,now=fixture.NOW,convert=False)
            anchor=seen[-1];mr.close(self.state,anchor,status='exhausted',reason='synthetic held',now=fixture.NOW)
            self.fx.request(need={'kind':'answer','question':'New priority item'},lane='initial_screen',subject=fixture.OTHER)
            ma.tick(self.state,now=fixture.NOW,convert=False)
        self.assertEqual(set(seen),{x['id']for x in requests})
        # tick has no object-local cursor; a fresh tick reloads the durable row.
        with ma._reader(self.state) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM material_acquisition_service_cursor').fetchone()[0],1)

    def test_empty_queue_keeps_existing_cursor(self):
        requests=self.requests(5)
        with mock.patch.object(ma,'serve',return_value={'outcome':'waiting'}):
            ma.tick(self.state,now=fixture.NOW,convert=False)
        with ma._reader(self.state)as c:
            before=dict(c.execute('SELECT * FROM material_acquisition_service_cursor').fetchone())
        for req in requests:mr.close(self.state,req['id'],status='exhausted',reason='synthetic',now=fixture.NOW)
        summary=ma.tick(self.state,now=fixture.NOW,convert=False)
        self.assertEqual(summary['open'],0);self.assertEqual(summary['served'],0)
        with ma._reader(self.state)as c:
            self.assertEqual(dict(c.execute('SELECT * FROM material_acquisition_service_cursor').fetchone()),before)

    def test_time_expiry_does_not_advance_unserved_items(self):
        requests=self.requests(5);seen=[];clock=[0.0]
        def serve(state,ref,**kwargs):seen.append(ref);clock[0]+=11;return {'request_id':ref,'outcome':'waiting'}
        with mock.patch.object(ma.time,'monotonic',side_effect=lambda:clock[0]),mock.patch.object(ma,'serve',side_effect=serve):
            first=ma.tick(self.state,now=fixture.NOW,convert=False,max_requests=4,max_seconds=10)
            second=ma.tick(self.state,now=fixture.NOW,convert=False,max_requests=4,max_seconds=10)
        ordered=sorted(requests,key=lambda x:ma.priority_key(x,{}))
        self.assertEqual(seen,[ordered[0]['id'],ordered[1]['id']])
        self.assertTrue(first['out_of_time']);self.assertTrue(second['out_of_time'])

    def test_time_expiring_while_acquiring_cursor_does_not_claim_or_serve(self):
        self.requests(5);clock=[0.0];writer=ma._writer
        @contextmanager
        def delayed_writer(state):
            with writer(state) as connection:
                clock[0]=11.0
                yield connection
        with mock.patch.object(ma.time,'monotonic',side_effect=lambda:clock[0]), \
                mock.patch.object(ma,'_writer',side_effect=delayed_writer),mock.patch.object(ma,'serve')as serve:
            summary=ma.tick(self.state,now=fixture.NOW,convert=False,max_seconds=10)
        serve.assert_not_called();self.assertEqual(summary['served'],0);self.assertTrue(summary['out_of_time'])
        with ma._reader(self.state)as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM material_acquisition_service_cursor').fetchone()[0],0)

    def test_pool_refusal_still_disables_costed_rungs_for_remainder_of_tick(self):
        self.requests(5);allows=[]
        def serve(state,ref,*,allow_costed,**kwargs):
            allows.append(allow_costed);return {'request_id':ref,'outcome':'budget_blocked','budget':{'scope':'pool'}}
        with mock.patch.object(ma,'serve',side_effect=serve):summary=ma.tick(self.state,now=fixture.NOW,convert=False)
        self.assertEqual(allows,[True,False,False,False]);self.assertEqual(summary['served'],4)

    def test_actual_cursor_cas_rejects_stale_claim_even_for_same_item_and_time(self):
        req=self.requests(1)[0]
        first=ma._advance_service_cursor(self.state,req,{},expected_hash=None,now=fixture.NOW)
        self.assertIsNotNone(first)
        self.assertIsNone(ma._advance_service_cursor(self.state,req,{},expected_hash=None,now=fixture.NOW))
        second=ma._advance_service_cursor(self.state,req,{},expected_hash=first,now=fixture.NOW)
        self.assertNotEqual(first,second)
        self.assertIsNone(ma._advance_service_cursor(self.state,req,{},expected_hash=first,now=fixture.NOW))

    def test_cursor_cas_conflict_never_calls_serve(self):
        self.requests(5)
        with mock.patch.object(ma,'_advance_service_cursor',return_value=None),mock.patch.object(ma,'serve')as serve:
            summary=ma.tick(self.state,now=fixture.NOW,convert=False)
        serve.assert_not_called();self.assertEqual(summary['served'],0);self.assertTrue(summary['scheduling_busy'])

    def test_single_request_error_does_not_prevent_later_service(self):
        requests=self.requests(5);seen=[]
        def serve(state,ref,**kwargs):
            seen.append(ref)
            if len(seen)==1:raise RuntimeError('synthetic source unavailable')
            return {'request_id':ref,'outcome':'waiting'}
        with mock.patch.object(ma,'serve',side_effect=serve):
            first=ma.tick(self.state,now=fixture.NOW,convert=False)
            second=ma.tick(self.state,now=fixture.NOW,convert=False)
        self.assertEqual(first['results'][0]['outcome'],'error')
        self.assertEqual(set(seen),{x['id']for x in requests});self.assertEqual(first['served'],4)
        self.assertLessEqual(second['served'],4)

    def test_corrupt_cursor_refuses_with_specific_diagnosis_and_no_calls(self):
        requests=self.requests(5)
        with mock.patch.object(ma,'serve',return_value={'outcome':'waiting'}):ma.tick(self.state,now=fixture.NOW,convert=False)
        with closing(sqlite3.connect(mr.ledger_path(self.state)))as c:
            c.execute("UPDATE material_acquisition_service_cursor SET content_hash=?",('0'*64,));c.commit()
        with mock.patch.object(ma,'serve')as serve,self.assertRaisesRegex(ma.MaterialAcquisitionError,'scheduling cursor content hash differs'):
            ma.tick(self.state,now=fixture.NOW,convert=False)
        serve.assert_not_called()
        for req in requests:self.assertEqual(mr.request(self.state,req['id'])['attempt_count'],0)
