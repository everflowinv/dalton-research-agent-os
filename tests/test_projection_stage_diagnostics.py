"""Diagnostic stages retain atomic writes, results and exact failure objects."""
import io,json,sqlite3,unittest
from contextlib import redirect_stderr
from unittest.mock import patch
from dalton_core import projection_stage_diagnostics as diagnostics
from dalton_core import dashboard_projector as projector_module
from dalton_core.plugins import static_dashboard
from tests import test_dashboard_projector as fixture

class ProjectionStageTests(unittest.TestCase):
    def setUp(self):
        self.case=fixture.DashboardProjectorTests(methodName='runTest')
        self.case.setUp();self.addCleanup(self.case.tearDown);self.addCleanup(self.case.doCleanups)
        self.root=self.case.core_path.parent
        self.projector=self.case.projector()
        self.destination=self.root/'timed-projection.sqlite'

    def events(self,stream):
        # stderr also carries fold diagnostics and unrelated runtime warnings.
        # Select this emitter's fixed prefix before JSON decoding; malformed
        # projection-stage records still fail rather than being suppressed.
        return [row for line in stream.getvalue().splitlines()
                if line.startswith('{"event":"projection_stage"')
                if (row:=json.loads(line)).get('event')=='projection_stage']

    def test_event_reader_isolates_unrelated_stderr_but_rejects_broken_stage_json(self):
        stream=io.StringIO()
        with redirect_stderr(stream):
            diagnostics.sys.stderr.write('ResourceWarning: unrelated SQLite cleanup\n\n')
            diagnostics.sys.stderr.write('{"event":"projection_fold_completed","rows":1}\n')
            with diagnostics.stage_timing('projection_write'):pass
        self.assertEqual([(e['stage'],e['status'])for e in self.events(stream)],
                         [('projection_write','started'),('projection_write','completed')])
        stream.write('{"event":"projection_stage","stage":\n')
        with self.assertRaises(json.JSONDecodeError):self.events(stream)

    def test_real_build_and_atomic_write_results_unchanged_and_logs_redacted(self):
        with patch.object(diagnostics,'_emit'):
            expected=self.projector.project(self.destination)
        stream=io.StringIO()
        with redirect_stderr(stream):actual=self.projector.project(self.destination)
        self.assertEqual(actual,expected)
        self.assertEqual([(e['stage'],e['status'])for e in self.events(stream)],
            [('build_snapshot','started'),('build_snapshot','completed'),
             ('projection_write','started'),('projection_write','completed')])
        for secret in [fixture.SECRET_PROMPT,fixture.SECRET_OUTPUT,str(self.destination)]:
            self.assertNotIn(secret,stream.getvalue())
        with sqlite3.connect(self.destination)as c:
            self.assertEqual(c.execute('SELECT source_watermark FROM projection_metadata').fetchone()[0],expected['metadata']['source_watermark'])

    def test_build_failure_keeps_same_exception_without_error_text(self):
        error=sqlite3.OperationalError('token=SECRET synthetic original body')
        stream=io.StringIO()
        with patch.object(self.projector,'_build_snapshot',side_effect=error),redirect_stderr(stream):
            with self.assertRaises(sqlite3.OperationalError)as caught:self.projector.build_snapshot()
        self.assertIs(caught.exception,error)
        events=self.events(stream)
        self.assertEqual([(e['stage'],e['status'])for e in events],[('build_snapshot','started'),('build_snapshot','error')])
        self.assertEqual(events[-1]['error_type'],'OperationalError')
        self.assertNotIn('SECRET',stream.getvalue())

    def test_write_failure_preserves_old_file_and_cleans_private_temporary(self):
        with patch.object(diagnostics,'_emit'):self.projector.project(self.destination)
        before=self.destination.read_bytes();error=RuntimeError('secret writer diagnostic body')
        stream=io.StringIO()
        with patch.object(projector_module.ProjectionWriter,'replace',side_effect=error),redirect_stderr(stream):
            with self.assertRaises(RuntimeError)as caught:self.projector.project(self.destination)
        self.assertIs(caught.exception,error);self.assertEqual(self.destination.read_bytes(),before)
        self.assertEqual(list(self.root.glob('.timed-projection.sqlite.*.tmp')),[])
        self.assertEqual(self.events(stream)[-1]['stage'],'projection_write')
        self.assertEqual(self.events(stream)[-1]['status'],'error')
        self.assertNotIn(str(error),stream.getvalue())

    def test_static_render_bytes_and_status_unchanged_and_failure_is_identical(self):
        with patch.object(diagnostics,'_emit'):
            self.projector.project(self.destination)
            expected=static_dashboard.render_static_dashboard(self.destination,self.root/'timed.html')
        before=(self.root/'timed.html').read_bytes();stream=io.StringIO()
        with redirect_stderr(stream):actual=static_dashboard.render_static_dashboard(self.destination,self.root/'timed.html')
        self.assertEqual(actual,expected);self.assertEqual((self.root/'timed.html').read_bytes(),before)
        self.assertEqual([(e['stage'],e['status'])for e in self.events(stream)],[('static_render','started'),('static_render','completed')])
        error=ValueError('secret raw static source');stream=io.StringIO()
        with patch.object(static_dashboard,'dashboard_snapshot',side_effect=error),redirect_stderr(stream):
            with self.assertRaises(ValueError)as caught:static_dashboard.render_static_dashboard(self.destination,self.root/'timed.html')
        self.assertIs(caught.exception,error);self.assertEqual((self.root/'timed.html').read_bytes(),before)
        self.assertEqual(self.events(stream)[-1]['status'],'error');self.assertNotIn(str(error),stream.getvalue())

    def test_broken_diagnostic_sink_changes_neither_success_nor_failure(self):
        class BrokenSink:
            def write(self,value):raise RuntimeError('unavailable diagnostic sink')
            def flush(self):raise RuntimeError('unavailable diagnostic sink')
        sentinel=object();error=ValueError('original failure')
        with patch.object(diagnostics.sys,'stderr',BrokenSink()):
            with diagnostics.stage_timing('build_snapshot'):result=sentinel
            self.assertIs(result,sentinel)
            with self.assertRaises(ValueError)as caught:
                with diagnostics.stage_timing('projection_write'):raise error
            self.assertIs(caught.exception,error)

    def test_fixed_stages_have_isolated_elapsed_budget_and_unknown_label_is_silent(self):
        stream=io.StringIO()
        with patch.object(diagnostics.time,'monotonic',side_effect=[10.,10.,12.]),redirect_stderr(stream):
            with diagnostics.stage_timing('build_snapshot'):pass
        events=self.events(stream);self.assertEqual(len(events),2)
        self.assertEqual(events[0]['elapsed_seconds'],0.);self.assertEqual(events[1]['elapsed_seconds'],2.)
        with patch.object(diagnostics,'_emit')as emit:
            with diagnostics.stage_timing('secret arbitrary caller label'):pass
        emit.assert_not_called()

if __name__=='__main__':unittest.main()
