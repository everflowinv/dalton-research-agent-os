"""AlphaEngine bounded probe: budget gate, authority presence, launcher path."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from dalton_core.bounded_alphaengine_probe import (
    ALPHAENGINE_PROFILE_REF,
    MAX_CALLS_PER_WINDOW,
    BoundedAlphaEngineProbeError,
    count_recent_alphaengine_calls,
    document_in_authority,
    execute_alphaengine_probe,
)
from dalton_core.alphaengine_acquisition_launcher import AcquisitionLaunchRejected
from dalton_core.store import DaltonStore, canonical_json, content_hash


NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def work_order(document_ref: str = "alphaengine-doc:130000095976806") -> dict:
    return {
        "schema_version": "0.1",
        "id": "work:bounded-planner:probe:ae",
        "metadata": {
            "permission_scope": "alphaengine_read",
            "operation": "alphaengine_get_document",
            "parameters": {
                "source_ref": "source:alphaengine",
                "locator": f"document/{document_ref}",
                "query_terms": ["transcript"],
            },
        },
    }


class FakeLauncher:
    def __init__(self, *, final_status: str = "succeeded") -> None:
        self.calls: list[dict] = []
        self.final_status = final_status

    def start_bounded_probe(self, *, document_ref, caller_ref, max_pages=20):
        self.calls.append({"document_ref": document_ref, "caller_ref": caller_ref})
        return {"id": "alphaengine-acquisition:test", "status": "running"}

    def status(self, ticket_ref):
        return {"id": ticket_ref, "status": self.final_status}


def seed_invocation(conn, *, hours_ago: float, count: int = 1) -> None:
    created = (NOW - timedelta(hours=hours_ago)).isoformat(timespec="microseconds")
    with conn:
        cur = conn.cursor()
        for index in range(count):
            cur.execute(
                "INSERT INTO connector_invocations"
                "(connector_invocation_id,execution_ref,execution_hash,work_order_ref,"
                "work_order_hash,connector_profile_ref,connector_profile_hash,"
                "call_spec_ref,call_spec_hash,capability_lease_ref,"
                "capability_lease_hash,descriptor_revision_ref,catalog_epoch,"
                "logical_invocation_key,record_json,content_hash,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"connector-invocation:ae:{hours_ago}:{index}", "exec:ae",
                    "0" * 64, "work:ae", "0" * 64, ALPHAENGINE_PROFILE_REF,
                    "0" * 64, "spec:ae", "0" * 64, None, None, None, None,
                    f"logical:ae:{hours_ago}:{index}",
                    canonical_json({"fixture": True}), "0" * 64, created,
                ),
            )


def seed_call_spec(conn, document_ref: str, *, with_page: bool = True) -> None:
    """Seed a get_document call spec and, by default, one successful page for it.

    ``document_in_authority`` requires a complete/partial SourceEnvelope bound
    to the call's invocation; a bare call spec (an attempt refused before any
    page was fetched) must not count.
    """

    with conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO connector_call_specs"
            "(call_spec_id,work_order_ref,work_order_hash,connector_profile_ref,"
            "operation,query_hash,record_json,content_hash,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                f"connector-call:{document_ref}", "work:ae", "0" * 64,
                ALPHAENGINE_PROFILE_REF, "get_document", "0" * 64,
                canonical_json({
                    "connector_profile_ref": ALPHAENGINE_PROFILE_REF,
                    "operation": "get_document",
                    "parameters": {"document_ref": document_ref},
                    "id": f"connector-call:{document_ref}",
                    "schema_version": "0.1",
                }),
                "0" * 64, NOW.isoformat(timespec="microseconds"),
            ),
        )
        if not with_page:
            return
        cur.execute(
            "INSERT INTO connector_invocations"
            "(connector_invocation_id,execution_ref,execution_hash,work_order_ref,"
            "work_order_hash,connector_profile_ref,connector_profile_hash,"
            "call_spec_ref,call_spec_hash,capability_lease_ref,"
            "capability_lease_hash,descriptor_revision_ref,catalog_epoch,"
            "logical_invocation_key,record_json,content_hash,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"connector-invocation:{document_ref}", f"exec:{document_ref}",
                "0" * 64, "work:ae", "0" * 64, ALPHAENGINE_PROFILE_REF,
                "0" * 64, f"connector-call:{document_ref}", "0" * 64, None, None, None, None,
                f"logical:{document_ref}", canonical_json({"fixture": True}), "0" * 64,
                (NOW - timedelta(hours=48)).isoformat(timespec="microseconds"),
            ),
        )
        cur.execute(
            "INSERT INTO connector_source_envelopes"
            "(source_envelope_id,connector_invocation_ref,connector_profile_ref,"
            "raw_artifact_version_ref,raw_response_hash,completeness,status,record_json,"
            "content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                f"source-envelope:{document_ref}", f"connector-invocation:{document_ref}",
                ALPHAENGINE_PROFILE_REF, "artifact:x", "0" * 64, "partial", "partial",
                canonical_json({"fixture": True}), "0" * 64,
                NOW.isoformat(timespec="microseconds"),
            ),
        )


class BoundedAlphaEngineProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        import sqlite3
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.executescript("""
        CREATE TABLE connector_invocations (
            connector_invocation_id TEXT PRIMARY KEY,
            execution_ref TEXT, execution_hash TEXT, work_order_ref TEXT,
            work_order_hash TEXT, connector_profile_ref TEXT,
            connector_profile_hash TEXT, call_spec_ref TEXT, call_spec_hash TEXT,
            capability_lease_ref TEXT, capability_lease_hash TEXT,
            descriptor_revision_ref TEXT, catalog_epoch TEXT,
            logical_invocation_key TEXT, record_json TEXT,
            content_hash TEXT, created_at TEXT
        );
        CREATE TABLE connector_call_specs (
            call_spec_id TEXT PRIMARY KEY, work_order_ref TEXT,
            work_order_hash TEXT, connector_profile_ref TEXT, operation TEXT,
            query_hash TEXT, record_json TEXT, content_hash TEXT, created_at TEXT
        );
        CREATE TABLE connector_source_envelopes (
            source_envelope_id TEXT PRIMARY KEY, connector_invocation_ref TEXT,
            connector_profile_ref TEXT, raw_artifact_version_ref TEXT,
            raw_response_hash TEXT, completeness TEXT, status TEXT,
            record_json TEXT, content_hash TEXT, created_at TEXT
        );
        """)

    def test_present_document_answers_from_authority_without_a_call(self) -> None:
        seed_call_spec(self.conn, "alphaengine-doc:130000095976806")
        launcher = FakeLauncher()
        envelope = execute_alphaengine_probe(
            work_order(), launcher=launcher, connection=self.conn,
            as_of=NOW,
        )
        self.assertEqual("succeeded", envelope["status"])
        self.assertEqual(
            [{"source_location": "alphaengine:alphaengine-doc:130000095976806"}],
            envelope["outputs"]["matches"],
        )
        self.assertEqual(0, envelope["metadata"]["calls_spent"])
        self.assertEqual([], launcher.calls)

    def test_bare_call_spec_without_page_is_not_authority(self) -> None:
        seed_call_spec(self.conn, "alphaengine-doc:7", with_page=False)
        self.assertFalse(document_in_authority(self.conn, "alphaengine-doc:7"))
        seed_call_spec(self.conn, "alphaengine-doc:8")
        self.assertTrue(document_in_authority(self.conn, "alphaengine-doc:8"))

    def test_budget_gate_refuses_without_spending_a_call(self) -> None:
        seed_invocation(self.conn, hours_ago=1, count=MAX_CALLS_PER_WINDOW)
        launcher = FakeLauncher()
        envelope = execute_alphaengine_probe(
            work_order("alphaengine-doc:2"), launcher=launcher,
            connection=self.conn, as_of=NOW,
        )
        self.assertEqual("failed", envelope["status"])
        self.assertEqual(
            "ALPHAENGINE_PROBE_BUDGET_EXCEEDED", envelope["error"]["code"]
        )
        self.assertEqual([], launcher.calls)

    def test_old_calls_outside_window_do_not_count(self) -> None:
        seed_invocation(self.conn, hours_ago=25, count=MAX_CALLS_PER_WINDOW)
        self.assertEqual(
            0, count_recent_alphaengine_calls(self.conn, as_of=NOW)
        )

    def test_missing_document_acquires_with_automation_principal(self) -> None:
        class Seeding(FakeLauncher):
            def __init__(self, store_conn):
                super().__init__()
                self.store_conn = store_conn
                self._seeded = False

            def status(self, ticket_ref):
                if not self._seeded:
                    seed_call_spec(self.store_conn, "alphaengine-doc:42")
                    self._seeded = True
                return {"id": ticket_ref, "status": "succeeded"}

        seeding = Seeding(self.conn)
        envelope = execute_alphaengine_probe(
            work_order("alphaengine-doc:42"), launcher=seeding,
            connection=self.conn, as_of=NOW,
        )
        self.assertEqual("succeeded", envelope["status"])
        self.assertEqual(1, envelope["metadata"]["calls_spent"])
        self.assertEqual(
            "automation:bounded-planner", seeding.calls[0]["caller_ref"]
        )

    def test_non_tier1_work_orders_are_rejected(self) -> None:
        for metadata in (
            {"permission_scope": "public_sec_read", "operation": "alphaengine_get_document"},
            {"permission_scope": "alphaengine_read", "operation": "get_company_facts"},
        ):
            work = {"id": "work:x", "metadata": metadata}
            with self.assertRaises(BoundedAlphaEngineProbeError):
                execute_alphaengine_probe(
                    work, launcher=FakeLauncher(),
                    connection=self.conn, as_of=NOW,
                )


if __name__ == "__main__":
    unittest.main()
