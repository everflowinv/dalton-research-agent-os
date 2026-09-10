from __future__ import annotations

import json
import multiprocessing
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from dalton_core.shared_connector_capacity import (
    SharedConnectorCapacityAuthority,
    SharedConnectorCapacityExceeded,
    policy_record,
)
from dalton_core.workspace import create_workspace_manifest
from dalton_core.workspace_runtime import ENVIRONMENT_KEY
from tests.test_connector_transport_executor import TransportHarness


def _reserve_process(database, policy_ref, policy_hash, workspace_id, now, start, queue):
    from datetime import datetime, timedelta
    try:
        authority = SharedConnectorCapacityAuthority(
            database, policy_ref=policy_ref, policy_hash=policy_hash,
            clock=lambda: datetime.fromisoformat(now))
        start.wait()
        authority.reserve(
            workspace_id=workspace_id, invocation_ref="connector-invocation:" + workspace_id,
            attempt_number=1, maximum_cost_micros=1000,
            expires_at=datetime.fromisoformat(now) + timedelta(hours=1))
        authority.close(); queue.put("ok")
    except Exception as exc:
        queue.put(type(exc).__name__)


class SharedConnectorCapacityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.host = Path(self.temp.name) / "Dalton"
        self.release = self.host / "runtime" / "releases" / ("e" * 64)
        self.release.mkdir(parents=True)
        probe = TransportHarness("success"); self.addCleanup(probe.close)
        self.scope = {
            "connector_ref": probe.base.profile["connector_ref"],
            "capability_ref": probe.base.profile["capability_id"],
            "credential_slot_refs": probe.base.profile["credential_slot_refs"],
        }
        self.now = probe.base.clock.value
        self.database = self.host / "fleet-capacity" / "connectors.sqlite"

    def policy(self, *, calls=2, concurrency=2, policy_id="connector-capacity:fixture"):
        return policy_record(
            id=policy_id, status="approved", **self.scope,
            provider_account_ref="provider-account:fixture-owner",
            rolling_window_seconds=86400, max_calls=calls,
            max_cost_micros=2000, max_cost_micros_per_call=1000,
            max_concurrency=concurrency, approved_by="human:owner",
            created_at=self.now.isoformat(timespec="microseconds"))

    def workspace(self, slug, port, policy):
        binding={"database":str(self.database),"policy_ref":policy["id"],
                 "policy_hash":policy["content_hash"]}
        return create_workspace_manifest(
            self.host, slug, port, "release:sha256:" + "e"*64, self.release,
            shared_connector_capacity=[binding])

    def test_two_workspaces_contend_and_replay_does_not_double_charge(self):
        policy=self.policy(calls=1); SharedConnectorCapacityAuthority.initialize(self.database,policy)
        one=self.workspace("analyst-a",8787,policy); two=self.workspace("analyst-b",8788,policy)
        first=TransportHarness("success"); self.addCleanup(first.close)
        with patch.dict("os.environ",{ENVIRONMENT_KEY:str(one.manifest_path)},clear=True):
            self.assertEqual(first.execute()["outcome"],"succeeded")
            self.assertEqual(first.execute()["idempotency_status"],"duplicate")
        second=TransportHarness("success"); self.addCleanup(second.close)
        with patch.dict("os.environ",{ENVIRONMENT_KEY:str(two.manifest_path)},clear=True):
            with self.assertRaises(SharedConnectorCapacityExceeded): second.execute()
        self.assertEqual(second.base.core.connection.execute(
            "SELECT count(*) FROM connector_physical_attempts").fetchone()[0], 0)
        con=SharedConnectorCapacityAuthority(self.database,policy_ref=policy["id"],policy_hash=policy["content_hash"])
        self.addCleanup(con.close)
        self.assertEqual(con.connection.execute("SELECT count(*) FROM shared_connector_capacity_reservations").fetchone()[0],1)

    def test_crash_after_dispatch_retains_concurrency(self):
        policy=self.policy(concurrency=1); SharedConnectorCapacityAuthority.initialize(self.database,policy)
        one=self.workspace("analyst-a",8787,policy); first=TransportHarness("success",fault_at="after_transport_started"); self.addCleanup(first.close)
        with patch.dict("os.environ",{ENVIRONMENT_KEY:str(one.manifest_path)},clear=True):
            with self.assertRaises(BaseException): first.execute()
            recovered=first.recovery_executor().recover(first.request()["id"])
            self.assertEqual(recovered["state"],"indeterminate_recovered")
        con=SharedConnectorCapacityAuthority(self.database,policy_ref=policy["id"],policy_hash=policy["content_hash"]); self.addCleanup(con.close)
        row=con.connection.execute("SELECT status,outcome,charged_micros FROM shared_connector_capacity_reservations").fetchone()
        self.assertEqual(tuple(row),("dispatched","transport_or_protocol_unknown",1000))

    def test_alphaengine_style_policy_uses_rolling_24_hours(self):
        policy=self.policy(calls=1); SharedConnectorCapacityAuthority.initialize(self.database,policy)
        clock=[self.now]
        authority=SharedConnectorCapacityAuthority(
            self.database,policy_ref=policy["id"],policy_hash=policy["content_hash"],
            clock=lambda: clock[0]); self.addCleanup(authority.close)
        first = authority.reserve(workspace_id="11111111-1111-4111-8111-111111111111",
                          invocation_ref="connector-invocation:first",attempt_number=1,
                          maximum_cost_micros=1000,expires_at=self.now+timedelta(hours=1))
        authority.mark_dispatched(first["reservation_ref"])
        # Crossing UTC midnight after one hour does not reset a rolling-day cap.
        clock[0]=self.now+timedelta(hours=2)
        with self.assertRaises(SharedConnectorCapacityExceeded):
            authority.reserve(workspace_id="22222222-2222-4222-8222-222222222222",
                              invocation_ref="connector-invocation:second",attempt_number=1,
                              maximum_cost_micros=1000,expires_at=clock[0]+timedelta(hours=1))
        clock[0]=self.now+timedelta(hours=25)
        authority.reserve(workspace_id="22222222-2222-4222-8222-222222222222",
                          invocation_ref="connector-invocation:second",attempt_number=1,
                          maximum_cost_micros=1000,expires_at=clock[0]+timedelta(hours=1))

    def test_two_processes_atomically_contend(self):
        policy=self.policy(calls=1); SharedConnectorCapacityAuthority.initialize(self.database,policy)
        context=multiprocessing.get_context("spawn"); start=context.Event(); queue=context.Queue()
        processes=[context.Process(target=_reserve_process,args=(
            str(self.database),policy["id"],policy["content_hash"],
            f"{number}1111111-1111-4111-8111-111111111111",self.now.isoformat(),start,queue))
            for number in (1,2)]
        for process in processes: process.start()
        start.set(); results=[queue.get(timeout=15) for _ in processes]
        for process in processes: process.join(15); self.assertEqual(process.exitcode,0)
        self.assertEqual(results.count("ok"),1)
        self.assertEqual(results.count("SharedConnectorCapacityExceeded"),1)

    def test_declared_missing_policy_refuses_before_physical_attempt(self):
        policy=self.policy(); workspace=self.workspace("analyst-a",8787,policy)
        harness=TransportHarness("success"); self.addCleanup(harness.close)
        with patch.dict("os.environ",{ENVIRONMENT_KEY:str(workspace.manifest_path)},clear=True):
            with self.assertRaisesRegex(Exception,"unavailable"): harness.execute()
        self.assertEqual(harness.base.core.connection.execute(
            "SELECT count(*) FROM connector_physical_attempts").fetchone()[0],0)

    def test_policy_version_does_not_reset_stable_account_scope(self):
        first=self.policy(calls=1,policy_id="connector-capacity:fixture:v1")
        second=self.policy(calls=1,policy_id="connector-capacity:fixture:v2")
        SharedConnectorCapacityAuthority.initialize(self.database,first)
        SharedConnectorCapacityAuthority.initialize(self.database,second)
        with SharedConnectorCapacityAuthority(self.database,policy_ref=first["id"],policy_hash=first["content_hash"],clock=lambda:self.now) as authority:
            authority.reserve(workspace_id="one",invocation_ref="connector-invocation:one",attempt_number=1,maximum_cost_micros=1000,expires_at=self.now+timedelta(hours=1))
        with SharedConnectorCapacityAuthority(self.database,policy_ref=second["id"],policy_hash=second["content_hash"],clock=lambda:self.now) as authority:
            with self.assertRaises(SharedConnectorCapacityExceeded):
                authority.reserve(workspace_id="two",invocation_ref="connector-invocation:two",attempt_number=1,maximum_cost_micros=1000,expires_at=self.now+timedelta(hours=1))


if __name__ == "__main__": unittest.main()
