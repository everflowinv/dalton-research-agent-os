"""Source reads cannot eat every normal targeted-search opportunity."""
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from datetime import datetime, timezone
from dalton_core.source_discovery_read_budget import bounded_rows, SourceReadDeadline
from dalton_core import mission_source_discovery as msd, material_acquisition as ma
from tests import test_material_targeted_sec as targeted


class ReadBudgetTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.db=Path(tmp.name)/'core.sqlite'
        self.c=sqlite3.connect(self.db);self.addCleanup(self.c.close);self.c.row_factory=sqlite3.Row
        self.c.execute('CREATE TABLE rows(v INTEGER)');self.c.executemany('INSERT INTO rows VALUES(?)',[(i,)for i in range(250)])
        self.c.commit()

    def test_single_large_sql_interrupts_with_original_handler_and_busy_unchanged(self):
        hits=[]
        self.c.execute('PRAGMA busy_timeout=4321')
        self.c.set_progress_handler(lambda:hits.append(True)or 0,1000)
        with self.assertRaises(SourceReadDeadline):
            bounded_rows(self.c,'SELECT sum(a.v*b.v*c.v) FROM rows a,rows b,rows c',deadline=time.monotonic()+.02)
        self.assertEqual(self.c.execute('PRAGMA busy_timeout').fetchone()[0],4321)
        # The original handler is still installed and authority writes still
        # succeed. No progress/timeout state was borrowed from this connection.
        old=len(hits);self.c.execute('SELECT sum(a.v*b.v) FROM rows a,rows b').fetchone()
        self.assertGreater(len(hits),old)
        self.c.execute('INSERT INTO rows VALUES(999)');self.c.commit()
        self.assertEqual(self.c.execute('SELECT MAX(v) FROM rows').fetchone()[0],999)

    def test_cumulative_small_reads_use_one_deadline_not_fresh_per_query_caps(self):
        clock=[0.0];completed=[]
        with patch('dalton_core.source_discovery_read_budget.time.monotonic',side_effect=lambda:clock[0]):
            for i in range(10):
                clock[0]+=.4
                try:bounded_rows(self.c,'SELECT v FROM rows LIMIT 1',deadline=1.0)
                except SourceReadDeadline:break
                completed.append(i)
        self.assertEqual(completed,[0,1])

    def test_unknown_sql_error_is_not_hidden_and_expiry_never_allows_a_write(self):
        with self.assertRaisesRegex(sqlite3.OperationalError,'no such table'):
            bounded_rows(self.c,'SELECT * FROM absent',deadline=time.monotonic()+1)
        with self.assertRaisesRegex(ValueError,'only supports SELECT'):
            bounded_rows(self.c,'DELETE FROM rows',deadline=time.monotonic()+1)
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM rows').fetchone()[0],250)
        with self.assertRaises(SourceReadDeadline):bounded_rows(self.c,'SELECT 1',deadline=time.monotonic()-1)

    def test_uncommitted_authority_is_deferred_not_read_from_stale_snapshot(self):
        self.c.execute('INSERT INTO rows VALUES(888)')
        with self.assertRaisesRegex(SourceReadDeadline,'awaits authority commit'):
            bounded_rows(self.c,'SELECT MAX(v) FROM rows',deadline=time.monotonic()+1)
        self.assertTrue(self.c.in_transaction)
        self.c.commit();self.assertEqual(bounded_rows(self.c,'SELECT MAX(v) FROM rows',deadline=time.monotonic()+1)[0][0],888)


    def test_busy_reader_is_deferred_without_changing_writer_busy_timeout(self):
        self.c.execute('PRAGMA busy_timeout=4321')
        with closing(sqlite3.connect(self.db)) as lock:
            lock.execute('BEGIN EXCLUSIVE')
            started=time.monotonic()
            with self.assertRaisesRegex(SourceReadDeadline,'busy'):
                bounded_rows(self.c,'SELECT v FROM rows',deadline=started+1)
            self.assertLess(time.monotonic()-started,.8)
            lock.rollback()
        self.assertEqual(self.c.execute('PRAGMA busy_timeout').fetchone()[0],4321)
        self.c.execute('INSERT INTO rows VALUES(777)');self.c.commit()



class TargetedOpportunityTests(unittest.TestCase):
    setUp=targeted.TargetedSecTests.setUp
    queued=targeted.TargetedSecTests.queued
    auth=targeted.TargetedSecTests.auth

    def coordinator(self):
        self.request,self.query=self.queued()
        self.clock=[1000.0];self.order=[]
        launcher=Mock(state_dir=self.state);launcher.running.return_value=False
        launcher.start.return_value={'id':'sec-filings-index:synthetic'}
        missions=Mock();missions.active_mission.return_value=self.mission
        missions.authorize_source_discovery.return_value=self.auth()
        missions.record_discovery_dispatch.return_value={'dispatch_id':'dispatch:synthetic'}
        missions.backfill_document_reviews.return_value=[]
        c=object.__new__(msd.MissionSourceDiscoveryCoordinator)
        c.search_launcher=launcher;c.missions=missions;c.source_ref=msd.SEC_SOURCE_REF
        c.plan=targeted.fixture.sec_plan();c.clock=lambda:datetime(2026,9,8,tzinfo=timezone.utc)
        c._health_hold=lambda:None;c._budget=lambda cap:{'remaining':1};c._missing_periods=lambda *a:[]
        c.tick_budget_seconds=20;c._search_deferred_ticks=0;c._retry_failures_now=False
        c.selection_launcher=None;c._tick_shared_view=msd._NO_TICK_CACHE
        def settled(kind):self.order.append(kind);return [{'status':'succeeded','paid_cost_preserved':17}]
        c.settle_dispatches=lambda deadline:settled('dispatch_settlement')
        c.settle_documents=lambda deadline:settled('paid_document_settlement')
        c.recover_local_web_discoveries=lambda **kw:[]
        def carry(deadline):
            self.order.append('carry')
            while True:
                self.clock[0]+=.4
                with closing(sqlite3.connect(self.state/'core.sqlite',isolation_level=None)) as reader:
                    bounded_rows(reader,'SELECT 1',deadline=deadline)
        c.carry_forward=carry;c.backfill_hosts=lambda **kw:{};c.settle_already_held=lambda deadline:[]
        c._acquire_within_budget=Mock(return_value=([],False,[]))
        c.launch_discovery=Mock(return_value={'status':'idle'})
        return c

    def test_actual_targeted_event_before_cumulative_maintenance_timeout(self):
        c=self.coordinator()
        with patch.object(msd,'_monotonic',side_effect=lambda:self.clock[0]),patch('dalton_core.source_discovery_read_budget.time.monotonic',side_effect=lambda:self.clock[0]):
            result=c.dispatch_once(1001.0)
        self.assertEqual(result['discovery']['status'],'launched')
        self.assertEqual(ma.targeted_query_status(self.state,self.query['query_id'])['status'],'launched')
        c.search_launcher.start.assert_called_once()
        self.assertEqual(c.search_launcher.start.call_args.kwargs['targeted_binding']['request_ref'],self.request['id'])
        self.assertEqual(self.order[:3],['dispatch_settlement','paid_document_settlement','carry'])
        self.assertEqual(result['settled_documents'][0]['paid_cost_preserved'],17)
        self.assertTrue(result['tick_budget_exhausted'])
        self.assertEqual(result['candidate_read_deferred']['step'],'carry')
        c._acquire_within_budget.assert_not_called();c.launch_discovery.assert_not_called()

    def test_expired_settlement_share_does_not_launch_and_next_normal_tick_does(self):
        c=self.coordinator();original=c.settle_dispatches
        def slow(deadline):self.clock[0]+=2;return original(deadline)
        c.settle_dispatches=slow
        with patch.object(msd,'_monotonic',side_effect=lambda:self.clock[0]),patch('dalton_core.source_discovery_read_budget.time.monotonic',side_effect=lambda:self.clock[0]):
            first=c.dispatch_once(1001.0)
            self.assertEqual(first['discovery']['status'],'deferred')
            c.search_launcher.start.assert_not_called()
            c.settle_dispatches=original;self.clock[0]=1300
            second=c.dispatch_once(1301.0)
        self.assertEqual(second['discovery']['status'],'launched')
        self.assertEqual(ma.targeted_query_status(self.state,self.query['query_id'])['status'],'launched')
        c.search_launcher.start.assert_called_once()


    def test_actual_writer_autocommit_targeted_candidate_does_not_permanently_defer(self):
        from dalton_core.store import DaltonStore
        from dalton_core.coverage_mission import CoverageMissionAuthority
        c=self.coordinator();store=DaltonStore(str(self.state/'core.sqlite'));self.addCleanup(store.close)
        c.store=store;c.missions=CoverageMissionAuthority(store)
        self.assertFalse(store.connection.in_transaction)
        with patch.object(msd,'_monotonic',side_effect=lambda:self.clock[0]),patch('dalton_core.source_discovery_read_budget.time.monotonic',side_effect=lambda:self.clock[0]):
            result=c.dispatch_once(1001.0)
        self.assertEqual(result['discovery']['status'],'launched')
        self.assertFalse(store.connection.in_transaction)
        self.assertEqual(ma.targeted_query_status(self.state,self.query['query_id'])['status'],'launched')

    def test_targeted_candidate_sql_deadline_surfaces_and_next_tick_records_event(self):
        c=self.coordinator()
        actual=bounded_rows
        def interrupted(reader,sql,parameters=(),**kwargs):
            if 'material_targeted_queries' in sql:
                raise SourceReadDeadline('synthetic candidate SQLite interrupted')
            return actual(reader,sql,parameters,**kwargs)
        with patch.object(msd,'_monotonic',side_effect=lambda:self.clock[0]),patch('dalton_core.source_discovery_read_budget.time.monotonic',side_effect=lambda:self.clock[0]):
            with patch('dalton_core.source_discovery_read_budget.bounded_rows',side_effect=interrupted):
                first=c.dispatch_once(1001.0)
            self.assertEqual(first['candidate_read_deferred']['step'],'search')
            c.search_launcher.start.assert_not_called()
            self.clock[0]=1300;second=c.dispatch_once(1301.0)
        self.assertEqual(second['discovery']['status'],'launched')
        self.assertEqual(ma.targeted_query_status(self.state,self.query['query_id'])['status'],'launched')
        c.search_launcher.start.assert_called_once()


    def test_archive_candidate_read_uses_the_same_deadline_not_a_new_share(self):
        from dalton_core.store import DaltonStore
        c=self.coordinator();store=DaltonStore(str(self.state/'core.sqlite'));self.addCleanup(store.close)
        c.store=store;actual=bounded_rows;seen=[]
        def interrupted(reader,sql,parameters=(),**kwargs):
            if 'JOIN material_targeted_query_events e' in sql:
                seen.append(kwargs['deadline'])
                raise SourceReadDeadline('synthetic archive SQLite interrupted')
            return actual(reader,sql,parameters,**kwargs)
        with patch.object(msd,'_monotonic',side_effect=lambda:self.clock[0]),patch('dalton_core.source_discovery_read_budget.time.monotonic',side_effect=lambda:self.clock[0]):
            with patch('dalton_core.source_discovery_read_budget.bounded_rows',side_effect=interrupted):
                first=c.dispatch_once(1001.0)
            self.assertEqual(first['candidate_read_deferred']['step'],'search')
            self.assertEqual(seen,[1001.0]);c.search_launcher.start.assert_not_called()
            self.clock[0]=1300;second=c.dispatch_once(1301.0)
        self.assertEqual(second['discovery']['status'],'launched')
        self.assertEqual(ma.targeted_query_status(self.state,self.query['query_id'])['status'],'launched')
