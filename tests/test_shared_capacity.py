from __future__ import annotations

import json
import multiprocessing
import os
import sqlite3
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from dalton_core.openclaw_model_adapter import (
    BrokerProtocolError, ModelAdmissionError, OpenClawModelAdapter,
)
from dalton_core.shared_capacity import (
    POLICY_SCHEMA_VERSION,
    SharedCapacityAuthority,
    SharedCapacityExceeded,
)
from dalton_core.store import content_hash
from dalton_core.workspace import create_workspace_manifest
from tests.test_openclaw_model_adapter import (
    AUTH_CLIENT_ID, AUTH_SECRET, FIXED_NOW, OpenClawModelAdapterTests,
    success_response,
)

NOW = datetime(2026, 9, 10, 12, tzinfo=timezone.utc)


def policy(*, calls=10, cost=10_000_000, concurrency=2):
    body = {
        "schema_version": POLICY_SCHEMA_VERSION,
        "id": "shared-capacity-policy:model-openai-main",
        "status": "approved", "provider": "openai",
        "credential_slot_ref": "credential-slot:openai:dalton",
        "max_daily_calls": calls, "max_daily_cost_micros": cost,
        "max_concurrency": concurrency, "approved_by": "human:owner",
        "created_at": NOW.isoformat(),
    }
    return {**body, "content_hash": content_hash(body)}


def reserve_process(database, policy_hash, workspace_id, start, queue):
    start.wait()
    try:
        with SharedCapacityAuthority(
            database, policy_ref="shared-capacity-policy:model-openai-main",
            policy_hash=policy_hash, clock=lambda: NOW,
        ) as authority:
            result = authority.reserve(
                workspace_id=workspace_id, invocation_ref="invocation:" + workspace_id[:8],
                provider="openai", credential_slot_ref="credential-slot:openai:dalton",
                maximum_cost_micros=100, expires_at=NOW + timedelta(minutes=1))
        queue.put(("ok", result["reservation_ref"]))
    except Exception as exc:
        queue.put((type(exc).__name__, str(exc)))


class SharedCapacityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.host = Path(self.temp.name) / "Dalton"
        self.db = self.host / "fleet-capacity" / "model.sqlite"

    def init(self, **kwargs):
        record = policy(**kwargs)
        SharedCapacityAuthority.initialize(self.db, record)
        return record

    def open(self, record, clock=lambda: NOW):
        return SharedCapacityAuthority(
            self.db, policy_ref=record["id"], policy_hash=record["content_hash"],
            clock=clock)

    def test_two_processes_atomically_contend_for_one_slot(self):
        record = self.init(concurrency=1)
        context = multiprocessing.get_context("spawn")
        start, queue = context.Event(), context.Queue()
        processes = [context.Process(
            target=reserve_process,
            args=(str(self.db), record["content_hash"], str(uuid.uuid4()), start, queue),
        ) for _ in range(2)]
        for process in processes:
            process.start()
        start.set()
        results = [queue.get(timeout=15) for _ in processes]
        for process in processes:
            process.join(15)
            self.assertEqual(process.exitcode, 0)
        self.assertEqual([item[0] for item in results].count("ok"), 1)
        self.assertEqual([item[0] for item in results].count("SharedCapacityExceeded"), 1)

    def test_idempotence_expiry_and_dispatched_conservatism(self):
        record = self.init(calls=2, concurrency=1)
        current = [NOW]
        with self.open(record, clock=lambda: current[0]) as authority:
            args = dict(workspace_id=str(uuid.uuid4()), invocation_ref="invocation:first",
                        provider="openai",
                        credential_slot_ref="credential-slot:openai:dalton",
                        maximum_cost_micros=100, expires_at=NOW + timedelta(seconds=1))
            first = authority.reserve(**args)
            self.assertEqual(authority.reserve(**args)["reservation_ref"],
                             first["reservation_ref"])
            current[0] += timedelta(seconds=2)
            second = authority.reserve(
                workspace_id=str(uuid.uuid4()), invocation_ref="invocation:second",
                provider="openai", credential_slot_ref="credential-slot:openai:dalton",
                maximum_cost_micros=100, expires_at=current[0] + timedelta(seconds=10))
            authority.mark_dispatched(second["reservation_ref"])
            current[0] += timedelta(days=2)
            with self.assertRaises(SharedCapacityExceeded):
                authority.reserve(
                    workspace_id=str(uuid.uuid4()), invocation_ref="invocation:third",
                    provider="openai", credential_slot_ref="credential-slot:openai:dalton",
                    maximum_cost_micros=100,
                    expires_at=current[0] + timedelta(seconds=10))

    def test_adapter_uses_workspace_identity_and_settles_once(self):
        record = self.init()
        release = self.host / "runtime" / "releases" / ("a" * 64)
        release.mkdir(parents=True)
        manifests = [create_workspace_manifest(
            self.host, f"analyst-{letter}", 8787 + index,
            "release:sha256:" + "a" * 64, release,
            shared_capacity={"database": str(self.db), "policy_ref": record["id"],
                             "policy_hash": record["content_hash"]},
        ) for index, letter in enumerate(("a", "b"))]
        invocation_ids = []
        for workspace in manifests:
            fixture = OpenClawModelAdapterTests("test_success_uses_only_admitted_authority_and_returns_uncommitted_contracts")
            fixture.setUp()
            try:
                with patch.dict(os.environ, {"DALTON_WORKSPACE_MANIFEST": str(workspace.manifest_path)}):
                    (invocation, _), broker = fixture.run_with(success_response)
                    broker.close()
                invocation_ids.append(invocation.id)
            finally:
                fixture.tearDown()
        self.assertEqual(len(set(invocation_ids)), 2)
        connection = sqlite3.connect(self.db)
        try:
            rows = connection.execute(
                "SELECT workspace_id,status,charged_micros,outcome FROM shared_capacity_reservations ORDER BY workspace_id"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row[1:] == ("settled", 10_000, "broker_succeeded")
                            for row in rows))

    def test_transport_failure_keeps_conservative_cost_and_never_calls_without_policy(self):
        record = self.init()
        release = self.host / "runtime" / "releases" / ("a" * 64)
        release.mkdir(parents=True)
        workspace = create_workspace_manifest(
            self.host, "analyst-a", 8787, "release:sha256:" + "a" * 64, release,
            shared_capacity={"database": str(self.db), "policy_ref": record["id"],
                             "policy_hash": record["content_hash"]})
        fixture = OpenClawModelAdapterTests("test_success_uses_only_admitted_authority_and_returns_uncommitted_contracts")
        fixture.setUp()
        try:
            with patch.dict(os.environ, {"DALTON_WORKSPACE_MANIFEST": str(workspace.manifest_path)}):
                with self.assertRaises(BrokerProtocolError):
                    fixture.run_with(lambda _request: b"not-json\n")
        finally:
            fixture.tearDown()
        connection = sqlite3.connect(self.db)
        try:
            row = connection.execute(
                "SELECT status,charged_micros,outcome FROM shared_capacity_reservations"
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(row, ("settled", 500000, "transport_or_protocol_unknown"))
        # Rebinding the manifest to a missing policy is detected before a new
        # broker transport can be reached.
        raw = json.loads(workspace.manifest_path.read_text())
        raw["shared_capacity"]["policy_ref"] = "shared-capacity-policy:missing"
        body = {key: value for key, value in raw.items() if key != "content_hash"}
        raw["content_hash"] = content_hash(body)
        workspace.manifest_path.write_text(json.dumps(raw))
        fixture = OpenClawModelAdapterTests("test_success_uses_only_admitted_authority_and_returns_uncommitted_contracts")
        fixture.setUp()
        try:
            adapter = OpenClawModelAdapter(
                Path(self.temp.name) / "must-not-connect.sock",
                route_resolver=fixture.router.get_decision,
                auth_client_id=AUTH_CLIENT_ID,
                auth_key_provider=lambda: AUTH_SECRET,
                clock=lambda: FIXED_NOW,
            )
            with patch.dict(os.environ, {"DALTON_WORKSPACE_MANIFEST": str(workspace.manifest_path)}):
                with patch.object(adapter, "_exchange",
                                  side_effect=AssertionError("transport reached")):
                    with self.assertRaisesRegex(ModelAdmissionError,
                                                "shared model capacity refused"):
                        adapter.execute(fixture.work, fixture.route, fixture.profile)
        finally:
            fixture.tearDown()


if __name__ == "__main__":
    unittest.main()
