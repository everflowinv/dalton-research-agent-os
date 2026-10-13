import io
import json
import unittest
from unittest.mock import patch

from dalton_core import source_discovery_diagnostics as diag
from dalton_core.mission_source_discovery import MissionSourceDiscoveryCoordinator
from dalton_core.writer_server import WriterServer


def coordinator():
    co = object.__new__(MissionSourceDiscoveryCoordinator)
    co.tick_budget_seconds = 20
    co._retry_failures_now = False
    co._search_deferred_ticks = 0
    co.source_ref = "source:alphaengine"
    co.plan = {"id": "sensitive-plan", "content_hash": "sensitive-hash"}
    co.selection_launcher = None
    for name in ("settle_dispatches", "recover_local_web_discoveries", "settle_documents",
                 "carry_forward", "settle_already_held"):
        setattr(co, name, lambda *args, **kwargs: [])
    co.backfill_hosts = lambda **kwargs: {}
    co.missions = type("Missions", (), {"backfill_document_reviews": lambda *args, **kwargs: []})()
    co.plan["mission_ref"] = "sensitive-mission"
    co._acquire_within_budget = lambda settled, deadline: ([], False, settled)
    co.launch_targeted_discovery = lambda deadline: None
    co.launch_discovery = lambda: {"status": "idle"}
    return co


def writer(co):
    server = object.__new__(WriterServer)
    server._source_discovery = co
    server._sec_filings_source_discovery = None
    server._web_source_discovery = None
    server._discovery_plan_error = None
    server._sec_filings_plan_error = None
    server._web_search_plan_error = None
    server._retry_pending_first_mission_sec_plan = lambda: None
    server._discovery_deferred_streaks = lambda: {}
    server._record_discovery_deferred_streaks = lambda *args: None
    return server


class DiscoveryDiagnosticsTests(unittest.TestCase):
    def test_actual_writer_and_coordinator_return_identical_result(self):
        # Only clocks and diagnostics differ: no mocked dispatch implementation.
        with patch("dalton_core.mission_source_discovery._monotonic", return_value=100):
            with patch.object(diag, "_emit"):
                expected = writer(coordinator())._op_dispatch_mission_source_discovery({"secret": "token"})
            stream = io.StringIO()
            with patch.object(diag.sys, "stderr", stream):
                actual = writer(coordinator())._op_dispatch_mission_source_discovery({"secret": "token"})
        self.assertEqual(actual, expected)
        rows = [json.loads(line) for line in stream.getvalue().splitlines()]
        self.assertLessEqual(len(rows), diag.MAX_LINES_PER_OPERATION)
        self.assertEqual({r["stage"] for r in rows},
                         {"first_mission_preflight", "coordinator", "settle", "carry", "held", "acquire", "search"})
        self.assertNotIn("sensitive", stream.getvalue())
        self.assertNotIn("secret", stream.getvalue())
        self.assertNotIn("token", stream.getvalue())

    def test_actual_stage_exception_propagates_unchanged_after_logging(self):
        co = coordinator()
        error = RuntimeError("secret request body and credential")
        def fail(*args):
            raise error
        co.settle_dispatches = fail
        stream = io.StringIO()
        with patch.object(diag.sys, "stderr", stream):
            with self.assertRaises(RuntimeError) as caught:
                writer(co)._op_dispatch_mission_source_discovery({})
        self.assertIs(caught.exception, error)
        rows = [json.loads(line) for line in stream.getvalue().splitlines()]
        self.assertEqual([(r["stage"], r["error_type"]) for r in rows if r["status"] == "error"],
                         [("settle", "RuntimeError"), ("coordinator", "RuntimeError")])
        self.assertNotIn(str(error), stream.getvalue())

    def test_sink_failure_does_not_change_return_or_original_exception(self):
        sink = unittest.mock.Mock()
        sink.write.side_effect = RuntimeError("broken logger")
        with patch.object(diag.sys, "stderr", sink):
            self.assertEqual(writer(coordinator())._op_dispatch_mission_source_discovery({})["status"], "idle")
            error = LookupError("original failure")
            with self.assertRaises(LookupError) as caught:
                with diag.diagnostic_scope(diag.DiagnosticBudget()), diag.phase("source:sec-edgar", "settle"):
                    raise error
        self.assertIs(caught.exception, error)

    def test_nested_repeated_phases_share_strict_limit_and_complete_reserved_pairs(self):
        stream = io.StringIO()
        budget = diag.DiagnosticBudget()
        with patch.object(diag.sys, "stderr", stream), diag.diagnostic_scope(budget):
            with diag.phase("source:alphaengine", "coordinator"):
                for _ in range(100):
                    with diag.phase("source:alphaengine", "settle"):
                        pass
        rows = [json.loads(line) for line in stream.getvalue().splitlines()]
        self.assertEqual(len(rows), diag.MAX_LINES_PER_OPERATION)
        self.assertEqual(sum(r["status"] == "started" for r in rows), 32)
        self.assertEqual(rows[-1]["stage"], "coordinator")
        self.assertEqual(rows[-1]["status"], "completed")

    def test_inactive_scope_and_unknown_labels_are_silent(self):
        stream = io.StringIO()
        with patch.object(diag.sys, "stderr", stream):
            with diag.phase("source:alphaengine", "settle"):
                pass
            with diag.diagnostic_scope(diag.DiagnosticBudget()), diag.phase("secret", "request body"):
                pass
        self.assertEqual(stream.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
