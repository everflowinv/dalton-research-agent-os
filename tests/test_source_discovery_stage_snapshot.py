"""A source decision turn reuses one proven view without changing authority."""
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from dalton_core import mission_source_discovery as msd
from dalton_core.source_discovery_read_budget import bounded_read_view, SourceReadDeadline

class StageSnapshotTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.db=Path(tmp.name)/'core.sqlite';self.c=sqlite3.connect(self.db,isolation_level=None)
        self.addCleanup(self.c.close);self.c.row_factory=sqlite3.Row
        self.c.execute('CREATE TABLE facts(value INTEGER)');self.c.execute('INSERT INTO facts VALUES(10)')
        self.co=object.__new__(msd.MissionSourceDiscoveryCoordinator)
        self.co.store=SimpleNamespace(connection=self.c)
        self.co._tick_stage_cache={};self.co._tick_read_deadline=time.monotonic()+10
        self.mission={'id':'mission-version:a:1','content_hash':'synthetic'}

    def test_same_turn_preserves_all_rows_and_order_with_one_validated_read(self):
        rows=[{'company_ref':'company:other','gaps':['quarter:2']},{'company_ref':'company:first','gaps':[]}]
        with patch('dalton_core.mission_stage.evaluate_mission',return_value=rows) as evaluate:
            for _ in range(12):self.assertEqual(self.co._stage_snapshot(self.mission),rows)
        self.assertEqual(evaluate.call_count,1)
        self.assertEqual(self.c.total_changes,1)

    def test_other_mission_version_and_changed_actual_input_do_not_share(self):
        with patch('dalton_core.mission_stage.evaluate_mission',return_value=[]) as evaluate:
            self.co._stage_snapshot(self.mission)
            self.co._stage_snapshot({**self.mission,'id':'mission-version:a:2'})
            self.co._stage_snapshot({**self.mission,'universe':[{'company_ref':'company:new'}]})
        self.assertEqual(evaluate.call_count,3)

    def test_authority_and_peer_commits_invalidate_cached_inputs(self):
        def evaluate(c,mission):return [c.execute('SELECT value FROM facts').fetchone()[0]]
        with patch('dalton_core.mission_stage.evaluate_mission',side_effect=evaluate) as reader:
            self.assertEqual(self.co._stage_snapshot(self.mission),[10])
            self.c.execute('UPDATE facts SET value=11')
            self.assertEqual(self.co._stage_snapshot(self.mission),[11])
            with closing(sqlite3.connect(self.db)) as peer:
                peer.execute('UPDATE facts SET value=12');peer.commit()
            self.assertEqual(self.co._stage_snapshot(self.mission),[12])
        self.assertEqual(reader.call_count,3)

    def test_outside_operation_has_original_reader_and_no_reuse(self):
        self.co._tick_stage_cache=None;self.co._tick_read_deadline=None
        with patch('dalton_core.mission_stage.evaluate_mission',return_value=[]) as reader:
            self.co._stage_snapshot(self.mission);self.co._stage_snapshot(self.mission)
        self.assertEqual(reader.call_count,2)
        self.assertIs(reader.call_args.args[0],self.c)

    def test_swallowed_interrupt_is_not_a_valid_empty_stage_view(self):
        self.c.executemany('INSERT INTO facts VALUES(?)',[(i,)for i in range(100)])
        def additive_legacy_reader(c,mission):
            try:c.execute('SELECT sum(a.value*b.value*c.value*d.value) FROM facts a,facts b,facts c,facts d').fetchone()
            except sqlite3.OperationalError:return []
        self.co._tick_read_deadline=time.monotonic()+.02
        with patch('dalton_core.mission_stage.evaluate_mission',side_effect=additive_legacy_reader):
            with self.assertRaises(SourceReadDeadline):self.co._stage_snapshot(self.mission)
        self.assertEqual(self.co._tick_stage_cache,{})
        self.c.execute('UPDATE facts SET value=13') # authority remains writable

    def test_existing_handler_and_busy_timeout_and_unknown_errors_survive(self):
        hits=[];self.c.execute('PRAGMA busy_timeout=4321');self.c.set_progress_handler(lambda:hits.append(1)or 0,1)
        with bounded_read_view(self.c,deadline=time.monotonic()+1) as reader:
            self.assertIsNot(reader,self.c);self.assertEqual(reader.execute('SELECT value FROM facts').fetchone()[0],10)
        self.assertEqual(self.c.execute('PRAGMA busy_timeout').fetchone()[0],4321)
        old=len(hits);self.c.execute('SELECT value FROM facts').fetchall();self.assertGreater(len(hits),old)
        with self.assertRaisesRegex(sqlite3.OperationalError,'absent'):
            with bounded_read_view(self.c,deadline=time.monotonic()+1) as reader:reader.execute('SELECT * FROM absent').fetchall()

    def test_cumulative_discovery_decisions_stop_before_new_paid_launch(self):
        clock=[100.0];co=self.co;co.plan={'companies':{'company:x':{}},'specs':[{'spec_ref':str(i)}for i in range(10)]}
        co.plan['mission_ref']='mission:x';co.missions=Mock();co.missions.active_mission.return_value={'universe':[{'company_ref':'company:x'}]}
        co.search_launcher=Mock();co.search_launcher.running.return_value=False;co._health_hold=lambda:None
        co._tick_read_deadline=101.0;seen=[]
        def blocked(*args):seen.append(args[-1]['spec_ref']);clock[0]+=.4;return 'existing exact cadence'
        co._spec_block=blocked
        with patch.object(msd,'_monotonic',side_effect=lambda:clock[0]):
            with self.assertRaises(SourceReadDeadline):co.launch_discovery()
        self.assertEqual(seen,['0','1','2']);co.search_launcher.start.assert_not_called()
        co.missions.authorize_source_discovery.assert_not_called()

    def test_cutoff_preserves_finished_settlement_and_clears_operation_view(self):
        co=self.co;co.tick_budget_seconds=20;co._search_deferred_ticks=0
        co._retry_failures_now=False;co.source_ref=msd.ALPHAENGINE_SOURCE_REF
        co.plan={'id':'plan:x','content_hash':'frozen','mission_ref':'mission:x'}
        co.missions=Mock();co.selection_launcher=None
        co.settle_dispatches=lambda deadline:[{'dispatch_ref':'dispatch:paid','settled_cost_micros':73}]
        co.recover_local_web_discoveries=lambda **kw:[]
        co.settle_documents=lambda deadline:[{'document_ref':'document:paid','status':'acquired'}]
        def cutoff(deadline):raise SourceReadDeadline('pure stage read exhausted')
        co.carry_forward=cutoff
        result=co.dispatch_once(time.monotonic()+1)
        self.assertEqual(result['settled_dispatches'][0]['settled_cost_micros'],73)
        self.assertEqual(result['settled_documents'][0]['status'],'acquired')
        self.assertTrue(result['tick_budget_exhausted'])
        self.assertEqual(result['candidate_read_deferred']['step'],'carry')
        self.assertIsNone(co._tick_stage_cache);self.assertIsNone(co._tick_read_deadline)

    def test_uncommitted_authority_never_uses_stale_disposable_view(self):
        self.c.execute('BEGIN');self.c.execute('UPDATE facts SET value=99')
        with self.assertRaisesRegex(SourceReadDeadline,'awaits authority commit'):
            self.co._stage_snapshot(self.mission)
        self.assertTrue(self.c.in_transaction);self.c.rollback()

if __name__=='__main__':unittest.main()
