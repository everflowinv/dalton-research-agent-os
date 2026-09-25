"""The core principal is granted every registered lane, whatever its token list says.

Live 2026-09-25: ``dispatch_quality_scoring`` (8deec67c) answered
``unavailable:forbidden`` on every tick in legacy and ws-7d from its deploy on.
The core principal's operation list in ``writer-tokens.json`` is only rewritten
by ``bootstrap`` (install.sh); a deploy is ``release_switch``, which never runs
it, so the list still held the 177 operations of the last install and the
writer refused the 178th to the principal bootstrap exists to grant it to.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from dalton_core.lane_registry import load_lanes
from dalton_core.writer_client import WriterClient
from dalton_core.writer_protocol import RemoteAuthorizationError
from dalton_core.writer_server import (
    CORE_OPERATIONS,
    DASHBOARD_CONTROL_OPERATIONS,
    Principal,
    load_principals,
    principal_may_call,
    write_token_config,
)

LANE = "dispatch_quality_scoring"


class PrincipalMayCallTests(unittest.TestCase):
    def setUp(self):
        load_lanes()
        self.assertIn(LANE, CORE_OPERATIONS)
        self.stale = frozenset(CORE_OPERATIONS - {LANE})

    def test_core_with_a_list_from_before_the_lane_may_call_it(self):
        core = Principal("core", "t", self.stale, unrestricted=True)
        self.assertTrue(principal_may_call(core, LANE))

    def test_core_gets_nothing_outside_core_operations(self):
        core = Principal("core", "t", self.stale, unrestricted=True)
        self.assertFalse(principal_may_call(core, "no_such_operation"))

    def test_every_other_principal_is_held_to_its_list(self):
        dashboard = Principal("dashboard-control", "t", DASHBOARD_CONTROL_OPERATIONS,
                              actor_ref="bridge:tailscale-dashboard")
        self.assertNotIn(LANE, DASHBOARD_CONTROL_OPERATIONS)
        self.assertFalse(principal_may_call(dashboard, LANE))
        listed = next(iter(DASHBOARD_CONTROL_OPERATIONS))
        self.assertTrue(principal_may_call(dashboard, listed))


class WriterSocketTests(unittest.TestCase):
    """End to end: the writer process reads a token file older than the lane."""

    def test_a_lane_newer_than_the_token_file_answers_the_core_principal(self):
        load_lanes()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        tokens = root / "private" / "writer-tokens.json"
        sock = root / "run" / "writer.sock"
        write_token_config(tokens, [
            Principal("core", "core-token-9f0c", frozenset(CORE_OPERATIONS - {LANE}),
                      unrestricted=True),
            Principal("dashboard-control", "dash-token-9f0c", DASHBOARD_CONTROL_OPERATIONS,
                      actor_ref="bridge:tailscale-dashboard"),
        ])
        self.assertNotIn(LANE, load_principals(tokens)["core"].operations)
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
        proc = subprocess.Popen([
            sys.executable, "-m", "dalton_core.writer_server",
            "--db", str(root / "private" / "core.sqlite"),
            "--scheduler", str(root / "scheduler.sqlite"),
            "--socket", str(sock), "--token-config", str(tokens),
            "--transcript-spool-dir", str(root / "transcript-spool"),
        ], cwd=str(Path(__file__).parents[1]), env=env,
           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        def stop():
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)

        self.addCleanup(stop)
        deadline = time.monotonic() + 30
        while not sock.exists():
            if proc.poll() is not None:
                self.fail(f"writer server exited with {proc.returncode}")
            if time.monotonic() > deadline:
                self.fail("writer server did not create socket")
            time.sleep(0.02)

        result = WriterClient(str(sock), "core-token-9f0c").call(LANE, {})
        # No judge configuration in this state directory, so the lane answers
        # for itself -- which is the point: it was reached, not refused.
        self.assertEqual(result["status"], "unconfigured")
        with self.assertRaises(RemoteAuthorizationError):
            WriterClient(str(sock), "dash-token-9f0c").call(LANE, {})


if __name__ == "__main__":
    unittest.main()
