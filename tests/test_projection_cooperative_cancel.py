"""Cancellation must abandon only derived work, preserving published and authority bytes."""
import hashlib,sqlite3,tempfile,threading,time,unittest
from pathlib import Path
from contextlib import closing
from unittest import mock
from concurrent.futures import Future
from dalton_core.dashboard import ProjectionWriter
from dalton_core.dashboard_projector import DashboardProjector,_open_read_only
from dalton_core.projection_cancellation import ProjectionCancelled
from dalton_core.service import DaltonService,ServiceConfig
from tests.test_dashboard import snapshot
from tests import test_dashboard_projector as fixture

def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()

class CancellationTests(unittest.TestCase):
 def setUp(self):
  self.case=fixture.DashboardProjectorTests();self.case.setUp();self.addCleanup(self.case.tearDown)
  self.root=self.case.core_path.parent;self.path=self.case.projection_path
  self.case.projector().project(self.path);self.before=(self.path.stat().st_ino,sha(self.path));self.authorities={p:sha(p) for p in [self.case.core_path,self.case.scheduler_path]}
 def unchanged(self):
  self.assertEqual((self.path.stat().st_ino,sha(self.path)),self.before)
  self.assertEqual({p:sha(p) for p in self.authorities},self.authorities)
  self.assertEqual(list(self.root.glob('.dashboard.sqlite.*')),[])
 def projector(self,event):
  return DashboardProjector(self.case.core_path,self.case.scheduler_path,clock=self.case.clock,cancel_requested=event.is_set)
 def test_pre_cancel_does_not_even_read_authorities(self):
  e=threading.Event();e.set()
  with mock.patch('dalton_core.dashboard_projector._open_read_only',side_effect=AssertionError('read after stop')):
   with self.assertRaises(ProjectionCancelled):self.projector(e).project(self.path)
  self.unchanged()
 def test_inflight_real_read_query_is_interrupted_and_source_stays_query_only(self):
  e=threading.Event();started=threading.Event();result=[]
  def read():
   try:
    with closing(_open_read_only(self.case.core_path,cancel_requested=e.is_set)) as c:
     self.assertEqual(c.execute('PRAGMA query_only').fetchone()[0],1);started.set()
     c.execute('WITH RECURSIVE x(n) AS (VALUES(1) UNION ALL SELECT n+1 FROM x WHERE n<100000000) SELECT sum(n) FROM x').fetchone()
   except sqlite3.OperationalError as exc:result.append(str(exc))
  t=threading.Thread(target=read);t.start();self.assertTrue(started.wait(2));e.set();t.join(2);self.assertFalse(t.is_alive());self.assertEqual(result,['interrupted']);self.unchanged()
 def test_cancel_mid_insert_rolls_back_temp_and_preserves_published_file(self):
  e=threading.Event();p=self.projector(e);s=snapshot();artifact=s['artifact_index'][0];s['artifact_index']=[{**artifact,'artifact_ref':'artifact:synthetic-'+str(i)} for i in range(5000)];p.build_snapshot=lambda:s
  real=ProjectionWriter;rows_seen=[]
  def writer(path,**kwargs):
   w=real(path,**kwargs)
   def trace(sql):
    if sql.startswith('INSERT INTO artifact_index'):
     rows_seen.append(True)
     if len(rows_seen)==5:e.set()
   w.connection.set_trace_callback(trace);return w
  with mock.patch('dalton_core.dashboard_projector.ProjectionWriter',writer):
   with self.assertRaises(ProjectionCancelled):p.project(self.path)
  self.assertGreaterEqual(len(rows_seen),5);self.unchanged()
 def test_cancel_after_completed_temp_before_atomic_replace_preserves_old_inode(self):
  e=threading.Event();p=self.projector(e);p.build_snapshot=snapshot
  import os
  real=os.fsync
  def sync(fd):real(fd);e.set()
  with mock.patch('dalton_core.dashboard_projector.os.fsync',sync):
   with self.assertRaises(ProjectionCancelled):p.project(self.path)
  self.unchanged()
 def test_no_cancel_retains_normal_complete_projection_semantics(self):
  e=threading.Event();a=self.case.projector().build_snapshot();b=self.projector(e).project(self.path);self.assertEqual(a,b)
  c=sqlite3.connect('file:'+str(self.path)+'?mode=ro',uri=True);c.execute('PRAGMA query_only=ON')
  self.assertEqual(c.execute('SELECT source_watermark FROM projection_metadata').fetchone()[0],b['metadata']['source_watermark']);c.close()
 def test_real_controller_stop_interrupts_source_query_and_joins_projection(self):
  e=threading.Event();raw={'schema_version':'0.1','core_db':str(self.case.core_path),'scheduler_db':str(self.root/'service-scheduler.sqlite'),'projection_db':str(self.path),'model_router_db':None,'capability_catalog_db':None,'heartbeat_path':str(self.root/'run/heartbeat.json'),'writer_socket':str(self.root/'run/writer.sock'),'tick_seconds':1,'projection_min_interval_seconds':1,'plugin_retry_seconds':1,'plugins':[]};service=DaltonService(ServiceConfig.from_mapping(raw));actual=DashboardProjector._build_snapshot
  def slow(p):
   with closing(_open_read_only(p.core_db,cancel_requested=p.cancel_requested)) as c:
    e.set();c.execute('WITH RECURSIVE x(n) AS (VALUES(1) UNION ALL SELECT n+1 FROM x WHERE n<100000000) SELECT sum(n) FROM x').fetchone()
   return actual(p)
  try:
   with mock.patch.object(DashboardProjector,'_build_snapshot',slow):
    service.start();service._ensure_projection_indexes();self.authorities={p:sha(p) for p in self.authorities};service._project(('synthetic',));self.assertTrue(e.wait(2));before=time.monotonic();service.stop();service.close();self.assertLess(time.monotonic()-before,2)
  finally:service.close()
  self.unchanged()
 def test_cancelled_future_is_not_a_new_ready_or_failure_projection(self):
  service=DaltonService.__new__(DaltonService);f=Future();f.set_exception(ProjectionCancelled('stopped'));service._projection_future=f;service._projection_error='previous error';service._last_projection_at='previous time';service._projection_watermark='previous hash';service._poll_projection();self.assertEqual(service._projection_error,'previous error');self.assertEqual(service._last_projection_at,'previous time');self.assertEqual(service._projection_watermark,'previous hash')

 def test_stop_before_build_cannot_begin_authority_index_maintenance(self):
  service=DaltonService.__new__(DaltonService);service._stop=threading.Event();service._stop.set()
  service._ensure_projection_indexes=mock.Mock(side_effect=AssertionError('must not write indexes after stop'))
  with self.assertRaises(ProjectionCancelled):service._build_projection(('synthetic',))
  service._ensure_projection_indexes.assert_not_called();self.unchanged()
 def test_unrelated_sql_error_is_not_hidden_as_cancellation(self):
  e=threading.Event();p=self.projector(e)
  def broken():
   with closing(_open_read_only(self.case.core_path,cancel_requested=e.is_set)) as c:c.execute('SELECT * FROM no_such_table').fetchone()
  p._build_snapshot=broken
  with self.assertRaisesRegex(sqlite3.OperationalError,'no such table'):p.project(self.path)
  self.unchanged()
