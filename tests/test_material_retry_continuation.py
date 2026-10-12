"""Maintenance progress is durable; it does not create source/paid authority."""
import json
import sqlite3
import unittest
from unittest import mock
from dalton_core import material_acquisition as ma, material_requests as mr
from dalton_core import material_source_queue_recovery as recovery
from tests.test_material_source_queue_recovery import QueueRecoveryTests, NOW, START

class RetryContinuationTests(unittest.TestCase):
    setUp = QueueRecoveryTests.setUp
    queued = QueueRecoveryTests.queued
    plan = QueueRecoveryTests.plan
    setup_query = QueueRecoveryTests.setup_query

    def ordinary(self, number, *, close=True):
        r = mr.open_request(self.state, subject='company:synthetic:other',
            need={'kind':'answer','question':f'Historical source {number}'},
            requester={'lane':'ask'}, now=START)
        if close: mr.close(self.state,r['id'],status='exhausted',reason='old miss',now=START)
        return r

    def cursor(self):
        with sqlite3.connect(mr.ledger_path(self.state)) as c:
            return json.loads(c.execute('SELECT record_json FROM material_acquisition_retry_cursor WHERE cursor_id=?',
                                        (ma._RETRY_CURSOR_ID,)).fetchone()[0])

    def test_real_queue_recovery_follows_repeated_ordinary_reopens_after_restart(self):
        earlier=[self.ordinary(i) for i in range(3)]
        r,q=self.setup_query(closed=True);events=r['events'];spend=ma.request_spend(self.state,r['id'])
        real=recovery.recover
        with mock.patch.object(ma,'retry_due',return_value=(True,'normal cache retry')):
            first=ma.retry_sweep(self.state,now=NOW)
            self.assertEqual(first[0]['request_id'],earlier[0]['id'])
            self.assertIn({'request_id':r['id'],'reason':recovery.RULE_REF},first)
            self.assertLessEqual(len(first),3)
            for v in earlier:
                if not mr.request(self.state,v['id'])['closed']:
                    mr.close(self.state,v['id'],status='exhausted',reason='still missing',now=NOW)
            # A separate call reads the persisted position, even while earlier
            # ordinary requests are due again. The real strong recovery runs.
            second=ma.retry_sweep(self.state,now=NOW)
        self.assertNotIn({'request_id':r['id'],'reason':recovery.RULE_REF},second)
        self.assertLessEqual(len(second),3)
        fresh=mr.request(self.state,r['id']);self.assertEqual(fresh['events'][:-1],events)
        self.assertEqual(ma.request_spend(self.state,r['id']),spend)
        self.assertEqual(sum('source_queue_recovery'in e for e in fresh['events']),1)

    def test_active_history_is_paged_without_full_history_fold(self):
        for i in range(ma._RETRY_PAGE_SIZE):self.ordinary(i,close=False)
        r=self.ordinary('last')
        ma.ensure_schema(self.state)
        with mock.patch.object(mr,'open_requests',side_effect=AssertionError('no full history fold')), \
             mock.patch.object(recovery,'recover',return_value=None), \
             mock.patch.object(ma,'retry_due',return_value=(True,'due')):
            second=ma.retry_sweep(self.state,now=NOW)
            self.assertEqual(self.cursor()['last_rowid'],ma._RETRY_PAGE_SIZE+1)
        self.assertEqual(second,[{'request_id':r['id'],'reason':'due'}])

    def test_cumulative_deadline_continues_after_last_attempt_not_first(self):
        rows=[self.ordinary(i) for i in range(4)];ma.ensure_schema(self.state)
        clock=[0.0];seen=[]
        def check(state,r,now):seen.append(r['id']);clock[0]+=.8;return None
        with mock.patch.object(ma.time,'monotonic',side_effect=lambda:clock[0]), \
             mock.patch.object(recovery,'recover',side_effect=check), \
             mock.patch.object(ma,'retry_due',return_value=(False,'not yet')):
            self.assertEqual(ma.retry_sweep(self.state,now=NOW),[])
            self.assertEqual(seen,[r['id'] for r in rows[:3]])
            seen.clear();ma.retry_sweep(self.state,now=NOW)
            self.assertEqual(seen[0],rows[3]['id'])

    def test_stale_claim_cas_cannot_rewind_or_repeat_a_turn(self):
        self.ordinary(1);self.ordinary(2);ma.ensure_schema(self.state)
        page,digest=ma._retry_page(self.state,deadline=ma.time.monotonic()+2)
        first=ma._claim_retry_turn(self.state,page[0],expected_hash=digest,now=NOW)
        self.assertIsNotNone(first)
        self.assertIsNone(ma._claim_retry_turn(self.state,page[0],expected_hash=digest,now=NOW))
        self.assertEqual(self.cursor()['revision'],1)

    def test_unverified_authority_is_reconsidered_after_wrap_without_new_grant(self):
        r,q=self.setup_query(closed=True)
        path=self.state/'discovery-plans'/'us-it-services-sec-filings-v1.json';old=path.read_text();path.unlink()
        self.assertEqual(ma.retry_sweep(self.state,now=NOW),[])
        path.write_text(old)
        self.assertEqual(ma.retry_sweep(self.state,now=NOW),[{'request_id':r['id'],'reason':recovery.RULE_REF}])

    def test_invalid_cursor_and_request_identity_fail_closed(self):
        self.ordinary(1);ma.ensure_schema(self.state)
        with mock.patch.object(recovery,'recover',return_value=None),mock.patch.object(ma,'retry_due',return_value=(False,'')):
            ma.retry_sweep(self.state,now=NOW)
        with sqlite3.connect(mr.ledger_path(self.state))as c:
            c.execute('UPDATE material_acquisition_retry_cursor SET content_hash=? WHERE cursor_id=?',('0'*64,ma._RETRY_CURSOR_ID))
        with self.assertRaises(ma.MaterialAcquisitionError):ma.retry_sweep(self.state,now=NOW)

    def test_nonpositive_limit_does_not_claim_or_reopen(self):
        self.ordinary(1)
        self.assertEqual(ma.retry_sweep(self.state,now=NOW,limit=0),[])
        self.assertEqual(ma.retry_sweep(self.state,now=NOW,limit=-1),[])

    def test_started_strong_recovery_finishes_but_no_next_item_after_deadline(self):
        r,q=self.setup_query(closed=True);other=self.ordinary('after')
        self.assertFalse(ma._claim_retry_first(self.state,now=NOW))
        clock=[0.0];real=recovery.recover
        def finish(*args,**kwargs):
            result=real(*args,**kwargs);clock[0]=3.0;return result
        with mock.patch.object(ma.time,'monotonic',side_effect=lambda:clock[0]), \
             mock.patch.object(recovery,'recover',side_effect=finish)as call:
            result=ma.retry_sweep(self.state,now=NOW)
        self.assertEqual(result,[{'request_id':r['id'],'reason':recovery.RULE_REF}])
        self.assertEqual(call.call_count,1)
        self.assertTrue(mr.request(self.state,other['id'])['closed'])

    def test_candidate_read_deadline_does_not_mutate_progress_or_authority(self):
        r=self.ordinary(1);events=mr.request(self.state,r['id'])['events']
        from dalton_core.source_discovery_read_budget import SourceReadDeadline
        with mock.patch.object(ma,'_rows',side_effect=SourceReadDeadline('interrupted')):
            self.assertEqual(ma.retry_sweep(self.state,now=NOW),[])
        self.assertEqual(mr.request(self.state,r['id'])['events'],events)

    def test_valid_hash_with_wrong_cursor_row_identity_is_not_authority(self):
        r=self.ordinary(1);other=self.ordinary(2);ma.ensure_schema(self.state)
        with mock.patch.object(recovery,'recover',return_value=None),mock.patch.object(ma,'retry_due',return_value=(False,'')):
            ma.retry_sweep(self.state,now=NOW)
        body=self.cursor();body['request_ref']=other['id']if body['request_ref']==r['id']else r['id']
        with sqlite3.connect(mr.ledger_path(self.state))as c:
            c.execute('UPDATE material_acquisition_retry_cursor SET record_json=?,content_hash=? WHERE cursor_id=?',
                      (ma._canonical(body),ma._sha(body),ma._RETRY_CURSOR_ID))
        with self.assertRaises(ma.MaterialAcquisitionError):ma.retry_sweep(self.state,now=NOW)

    def test_both_classes_get_turns_under_repeated_earlier_normal_arrivals(self):
        normal=[self.ordinary(i)for i in range(20)]
        queued=[]
        for i in range(7):
            r=self.ordinary(f'queue-{i}',close=False)
            # Synthetic queue-shaped history exercises cheap classification;
            # the separate real recovery test proves its authority validation.
            mr.record_attempt(self.state,r['id'],source='sec_edgar',outcome='not_connected',
                              detail=recovery.LEGACY_TIMEOUT_DETAIL,now=START)
            mr.close(self.state,r['id'],status='exhausted',reason='legacy queue timeout',now=START)
            queued.append(r)
        reached=set();queue_ids={r['id']for r in queued}
        def qualified(state,r,now):
            if r['id']not in queue_ids:return None
            reached.add(r['id'])
            mr.reopen(state,r['id'],reason='synthetic qualified queue scheduling',now=now)
            return {'rule_ref':recovery.RULE_REF}
        with mock.patch.object(recovery,'recover',side_effect=qualified),mock.patch.object(ma,'retry_due',return_value=(True,'arrival')):
            for tick in range(4):
                result=ma.retry_sweep(self.state,now=NOW)
                self.assertLessEqual(len(result),3)
                if tick%2==0:self.assertTrue(any(v['request_id']not in queue_ids for v in result))
                for r in normal:
                    if not mr.request(self.state,r['id'])['closed']:
                        mr.close(self.state,r['id'],status='exhausted',reason='same miss',now=NOW)
        self.assertEqual(reached,queue_ids)

    def test_slow_normal_cannot_starve_queue_next_tick_even_with_one_slot(self):
        self.ordinary('earlier');r,q=self.setup_query(closed=True)
        clock=[0.0];real=recovery.recover
        def slow_normal(*args,**kwargs):clock[0]+=3;return False,'not due'
        with mock.patch.object(ma.time,'monotonic',side_effect=lambda:clock[0]), \
             mock.patch.object(ma,'retry_due',side_effect=slow_normal):
            self.assertEqual(ma.retry_sweep(self.state,now=NOW,limit=1),[])
            second=ma.retry_sweep(self.state,now=NOW,limit=1)
        self.assertEqual(second,[{'request_id':r['id'],'reason':recovery.RULE_REF}])

    def test_slow_queue_cannot_starve_normal_next_tick_after_restart(self):
        ordinary=self.ordinary('normal');r,q=self.setup_query(closed=True)
        # Complete the initial ordinary-first turn, so the next starts queue.
        with mock.patch.object(recovery,'recover',return_value=None),mock.patch.object(ma,'retry_due',return_value=(False,'')):
            ma.retry_sweep(self.state,now=NOW,limit=1)
        clock=[0.0]
        def slow_queue(state,view,now):
            if view['id']==r['id']:clock[0]+=3
            return None
        with mock.patch.object(ma.time,'monotonic',side_effect=lambda:clock[0]), \
             mock.patch.object(recovery,'recover',side_effect=slow_queue), \
             mock.patch.object(ma,'retry_due',return_value=(True,'normal due')):
            self.assertEqual(ma.retry_sweep(self.state,now=NOW,limit=1),[])
            # No retained launcher/process state: the ledger alone rotates.
            next_tick=ma.retry_sweep(self.state,now=NOW,limit=1)
        self.assertEqual(next_tick,[{'request_id':ordinary['id'],'reason':'normal due'}])

    def test_rotation_hash_tampering_is_not_a_free_reset(self):
        self.ordinary(1);ma.retry_sweep(self.state,now=NOW)
        with sqlite3.connect(mr.ledger_path(self.state))as c:
            c.execute("UPDATE material_acquisition_retry_cursor SET content_hash=? WHERE cursor_id='retry-class-rotation'",('0'*64,))
        with self.assertRaises(ma.MaterialAcquisitionError):ma.retry_sweep(self.state,now=NOW)
