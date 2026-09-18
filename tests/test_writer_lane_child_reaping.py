"""WP-B1-5: the writer reaps every launcher's children, not only the ticking one.

``reap()`` existed and nothing called it.  A lane child's handle was polled in
exactly two places -- ``spawn``, just before it took the slot back, and the
``status`` call the lane's own coordinator makes about its one open ticket --
so a launcher whose lane was held, unconfigured, idle or simply between
tickets never waited on anything.  Live that left thirteen ``<defunct>``
children parked under three writer processes for hours.

Reaping is a fact about this process's children and has nothing to do with
which lane happens to be ticking, so every lane tick now reaps every launcher.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dalton_core.lane_child_launcher import LaneChildLauncher
from dalton_core.writer_server import LaneShortCircuit, WriterServer


class _Probe(LaneChildLauncher):
    TICKET_PREFIX = "probe-run"
    TICKETS_DIRNAME = "probe-runs"

    def _command(self, *, ticket_dir: Path) -> list[str]:
        return [self.python_executable, "-c", "import sys; sys.exit(0)"]


class _Lane:
    def __init__(self, operation, handler):
        self.operation = operation
        self.handler = handler
        self.init_kwarg = None


class _Broken:
    def reap(self):
        raise RuntimeError("this launcher is wedged")


class WriterReapsEveryLauncherTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.state = Path(self._dir.name) / "state"
        self.state.mkdir()

    def _launcher(self):
        made = _Probe(state_dir=self.state)
        self.addCleanup(made.close)
        return made

    def _server(self, **launchers):
        server = object.__new__(WriterServer)
        server.lane_holds = LaneShortCircuit()
        server._lane_deadline = None
        server._lane_launchers = dict(launchers)
        return server

    def _finished_child(self, launcher, digest):
        ticket = launcher.spawn(digest=digest, record={})
        launcher.wait(timeout=30)
        return ticket

    def test_a_lane_tick_reaps_a_launcher_whose_own_lane_never_ran(self):
        quiet = self._launcher()
        ticket = self._finished_child(quiet, "a" * 24)
        self.assertIn(ticket["id"], quiet._children)

        server = self._server(probe_launcher=quiet)
        with mock.patch("dalton_core.writer_server.lane_for_operation",
                        return_value=None):
            # Some *other* lane ticks.  The quiet launcher's finished child is
            # still this writer's child and is waited on all the same.
            server._run_lane(
                _Lane("dispatch_prior_research", lambda s, p: {"status": "idle"}), {})
        self.assertEqual(quiet._children, {})
        settled = json.loads(
            quiet._ticket_path(ticket["id"]).read_text(encoding="utf-8"))
        self.assertEqual(settled["status"], "succeeded")
        self.assertEqual(settled["exit_code"], 0)

    def test_a_lane_that_raises_still_reaps(self):
        quiet = self._launcher()
        ticket = self._finished_child(quiet, "b" * 24)
        server = self._server(probe_launcher=quiet)

        def boom(_server, _params):
            raise TimeoutError("store is busy")

        with mock.patch("dalton_core.writer_server.lane_for_operation",
                        return_value=None):
            with self.assertRaises(TimeoutError):
                server._run_lane(_Lane("dispatch_prior_research", boom), {})
        self.assertEqual(quiet._children, {})
        self.assertEqual(quiet.status(ticket["id"])["status"], "succeeded")

    def test_launchers_wired_by_name_are_reaped_too(self):
        named = self._launcher()
        ticket = self._finished_child(named, "c" * 24)
        server = self._server()
        server._web_fetch_launcher = named
        self.assertEqual(server.reap_lane_children(), [ticket["id"]])
        self.assertEqual(named._children, {})

    def test_one_launcher_it_cannot_reap_never_stops_the_others(self):
        good = self._launcher()
        ticket = self._finished_child(good, "d" * 24)
        server = self._server(broken_launcher=_Broken(), probe_launcher=good)
        self.assertEqual(server.reap_lane_children(), [ticket["id"]])

    def test_the_same_launcher_wired_twice_is_reaped_once(self):
        shared = self._launcher()
        server = self._server(one=shared, two=shared)
        server._acquisition_launcher = shared
        self.assertEqual(
            [launcher for launcher in server.lane_launchers()
             if launcher is shared], [shared])

    def test_a_launcher_with_no_reap_is_skipped_rather_than_failing(self):
        server = self._server(no_reap=object())
        self.assertEqual(server.reap_lane_children(), [])

    def test_a_still_running_child_is_left_alone(self):
        launcher = _Probe(state_dir=self.state)
        self.addCleanup(launcher.close)
        launcher._command = lambda *, ticket_dir: [
            launcher.python_executable, "-c", "import time; time.sleep(30)"]
        ticket = launcher.spawn(digest="e" * 24, record={})
        server = self._server(probe_launcher=launcher)
        self.assertEqual(server.reap_lane_children(), [])
        self.assertIn(ticket["id"], launcher._children)


if __name__ == "__main__":
    unittest.main()
