from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from dalton_core.governance_cli import ephemeral_call
from dalton_core.writer_server import (
    CORE_OPERATIONS, DASHBOARD_CONTROL_OPERATIONS, FEEDBACK_BRIDGE_OPERATIONS,
    Principal, load_principals, write_token_config,
)


class GovernanceCliTests(unittest.TestCase):
    def test_first_mission_publish_allows_for_bounded_materialization(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tokens = root / "tokens.json"
            write_token_config(tokens, [
                Principal("core", "core-secret-token", CORE_OPERATIONS, unrestricted=True),
            ])
            observed = {}

            class InspectingClient:
                def __init__(self, socket_path, token, timeout):
                    observed["timeout"] = timeout

                def call(self, operation, params):
                    return {"status": "ok"}

            with patch("dalton_core.governance_cli.WriterClient", InspectingClient):
                ephemeral_call(
                    tokens, root / "writer.sock", actor_ref="human:owner",
                    operation="publish_first_workspace_mission", params={})
            self.assertEqual(observed["timeout"], 45)

    def test_ephemeral_principal_contains_only_selected_operation_and_restores_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tokens = root / "tokens.json"
            write_token_config(tokens, [
                Principal("core", "core-secret-token", CORE_OPERATIONS, unrestricted=True),
                Principal(
                    "dashboard-control", "legacy-dashboard-token",
                    DASHBOARD_CONTROL_OPERATIONS - {"publish_first_workspace_mission"},
                    actor_ref="bridge:tailscale-dashboard"),
                Principal(
                    "agenda-timeout", "legacy-timeout-token",
                    FEEDBACK_BRIDGE_OPERATIONS,
                    actor_ref="automation:agenda-timeout"),
            ])
            original = tokens.read_bytes()

            class InspectingClient:
                def __init__(self, socket_path, token, timeout):
                    self.token = token

                def call(self, operation, params):
                    principals = load_principals(
                        tokens, allow_managed_operation_subset=True)
                    human = [p for p in principals.values()
                             if p.resolved_actor_ref == "human:owner"]
                    self_test.assertEqual(len(human), 1)
                    self_test.assertEqual(human[0].operations, frozenset({operation}))
                    self_test.assertEqual(human[0].token, self.token)
                    return {"status": "ok"}

            self_test = self
            with patch("dalton_core.governance_cli.WriterClient", InspectingClient):
                result = ephemeral_call(
                    tokens, root / "writer.sock", actor_ref="human:owner",
                    operation="create_agenda_policy", params={})
            self.assertEqual(result, {"status": "ok"})
            self.assertEqual(tokens.read_bytes(), original)
            restored = load_principals(
                tokens, allow_managed_operation_subset=True)
            self.assertEqual(set(restored), {"core", "dashboard-control", "agenda-timeout"})

    def test_ephemeral_human_token_is_removed_after_one_operation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "private" / "core.sqlite"
            socket = root / "run" / "writer.sock"
            tokens = root / "private" / "tokens.json"
            write_token_config(tokens, [
                Principal("core", "core-secret-token", CORE_OPERATIONS, unrestricted=True),
            ])
            env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
            process = subprocess.Popen(
                [sys.executable, "-m", "dalton_core.writer_server", "--db", str(db),
                 "--socket", str(socket), "--token-config", str(tokens)],
                cwd=str(Path(__file__).parents[1]), env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            try:
                deadline = time.time() + 5
                while time.time() < deadline and not socket.exists():
                    if process.poll() is not None:
                        self.fail("writer server exited")
                    time.sleep(0.02)
                result = ephemeral_call(
                    tokens, socket,
                    actor_ref="human:owner",
                    operation="create_agenda_policy",
                    params={
                        "policy": {
                            "schema_version": "0.1", "enabled": False,
                            "selected_count": 1, "max_model_calls_per_cycle": 1,
                            "max_daily_cycles": 1, "max_daily_cost_usd": 0.1,
                            "max_monthly_cost_usd": 1.0, "max_input_tokens": 1000,
                            "max_output_tokens": 100,
                            "feature_weights": {"mandate_relevance": 1, "catalyst_urgency": 1, "evidence_staleness": 1, "decision_impact": 1},
                            "trial_company_refs": ["wanhua"], "cutover_enabled": False,
                            "cutover_acceptance_threshold": None,
                        },
                        "effective_from": "2026-08-14T00:00:00+00:00",
                        "effective_until": None,
                        "activate": True,
                        "version_id": "agenda-policy-version:test",
                        "idempotency_key": "agenda-policy:test",
                    },
                )
                self.assertEqual(result["actor_ref"], "human:owner")
                principals = load_principals(tokens)
                self.assertEqual(set(principals), {"core"})
                self.assertFalse(any(p.resolved_actor_ref.startswith("human:") for p in principals.values()))
            finally:
                process.terminate()
                process.wait(timeout=3)


if __name__ == "__main__":
    unittest.main()
