from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import timedelta

from dalton_core.credential_authority import (
    CredentialAuthorityStore,
    CredentialGrantEnvelope,
    CredentialGrantRejected,
)
from dalton_core.store import DaltonStore, content_hash
from tests.test_connector import MutableClock, WHEN, assert_wire_schema


def grant_spec(*, max_calls: int = 2) -> dict:
    base = {
        "schema_version": "0.1",
        "id": "credential-grant:alphaengine:test",
        "created_at": WHEN.isoformat(timespec="microseconds"),
        "expires_at": (WHEN + timedelta(minutes=5)).isoformat(
            timespec="microseconds"
        ),
        "authority_ref": "credential-authority:host:alphaengine",
        "grant_kind": "mcp_managed",
        "target_ref": "mcp-target:alphaengine",
        "connector_profile_ref": "connector-profile:alphaengine:v1",
        "connector_profile_hash": "1" * 64,
        "capability_lease_ref": "capability-lease:alphaengine:test",
        "capability_lease_hash": "2" * 64,
        "adapter_ref": "mcp-target:alphaengine",
        "adapter_hash": "3" * 64,
        "principal_ref": "principal:worker-1",
        "credential_slot_refs": ["credential-slot:alphaengine"],
        "allowed_operations": ["search_library"],
        "max_calls": max_calls,
    }
    return {**base, "content_hash": content_hash(base)}


def authority() -> dict:
    return {
        "connector_profile_ref": "connector-profile:alphaengine:v1",
        "connector_profile_hash": "1" * 64,
        "capability_lease_ref": "capability-lease:alphaengine:test",
        "capability_lease_hash": "2" * 64,
        "adapter_ref": "mcp-target:alphaengine",
        "adapter_hash": "3" * 64,
        "principal_ref": "principal:worker-1",
        "credential_slot_refs": ["credential-slot:alphaengine"],
        "operation": "search_library",
    }


def use_authority(number: int = 1) -> dict:
    return {
        "runner_request_ref": f"connector-runner-request:alphaengine:{number}",
        "runner_request_hash": "4" * 64,
        "connector_invocation_ref": f"connector-invocation:alphaengine:{number}",
        "connector_invocation_hash": "5" * 64,
        "reservation_ref": f"connector-quota-reservation:alphaengine:{number}",
        "reservation_hash": "6" * 64,
        "physical_attempt_number": number,
    }


class CredentialAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = MutableClock()
        self.store = DaltonStore(":memory:")
        self.handle = object()
        self.authority = CredentialAuthorityStore(
            self.store, handle_resolver=lambda grant: self.handle, clock=self.clock
        )
        self.addCleanup(self.store.close)

    def register(self, *, max_calls: int = 2) -> dict:
        return self.authority.register_grant(
            grant_spec(max_calls=max_calls), idempotency_key="grant:alphaengine"
        )

    def authorize(self, number: int = 1) -> dict:
        return self.authority.authorize_use(
            **use_authority(number),
            **authority(),
            idempotency_key=f"use:alphaengine:{number}",
        )

    def test_closed_grant_use_and_revocation_contracts(self) -> None:
        grant = self.register()
        self.assertEqual(grant["write_status"], "fresh")
        duplicate = self.register()
        self.assertEqual(duplicate["write_status"], "duplicate")
        self.assertEqual(
            CredentialGrantEnvelope.from_dict(
                {key: value for key, value in grant.items() if key != "write_status"}
            ).content_hash,
            grant["content_hash"],
        )
        assert_wire_schema(self, "credential-grant-envelope.schema.json", grant)
        use = self.authorize()
        assert_wire_schema(self, "credential-use-receipt.schema.json", use)
        duplicate_use = self.authorize()
        self.assertEqual(duplicate_use["write_status"], "duplicate")
        authorized = self.authority.validate_use(
            use["id"],
            use_hash=use["content_hash"],
            **use_authority(),
            **authority(),
        )
        self.assertIs(authorized.handle, self.handle)
        self.assertEqual(authorized.grant.id, grant["id"])
        revoked = self.authority.revoke(
            grant["id"],
            grant_hash=grant["content_hash"],
            reason_code="manual",
            actor_ref="human:governance",
            idempotency_key="revoke:alphaengine",
        )
        assert_wire_schema(self, "credential-revocation-event.schema.json", revoked)
        with self.assertRaises(CredentialGrantRejected):
            self.authority.peek_for_use(**authority())
        with self.assertRaises(CredentialGrantRejected):
            self.authority.validate_use(
                use["id"],
                use_hash=use["content_hash"],
                **use_authority(),
                **authority(),
            )

    def test_max_calls_is_counted_by_durable_authorization_receipts(self) -> None:
        self.register(max_calls=1)
        self.authorize(1)
        with self.assertRaisesRegex(CredentialGrantRejected, "max_calls"):
            self.authority.peek_for_use(**authority())
        with self.assertRaisesRegex(CredentialGrantRejected, "max_calls"):
            self.authorize(2)

    def test_expiry_and_exact_authority_fail_closed(self) -> None:
        self.register()
        for field, value in (
            ("connector_profile_hash", "9" * 64),
            ("capability_lease_hash", "9" * 64),
            ("adapter_ref", "mcp-target:other"),
            ("principal_ref", "principal:other"),
            ("credential_slot_refs", ["credential-slot:other"]),
            ("operation", "get_document"),
        ):
            changed = {**authority(), field: value}
            with self.subTest(field=field), self.assertRaises(CredentialGrantRejected):
                self.authority.peek_for_use(**changed)
        self.clock.value = WHEN + timedelta(minutes=5)
        with self.assertRaises(CredentialGrantRejected):
            self.authority.peek_for_use(**authority())

    def test_receipt_authority_cannot_be_rebound(self) -> None:
        self.register()
        use = self.authorize()
        for changed in (
            {**use_authority(), "reservation_hash": "9" * 64},
            {**use_authority(), "physical_attempt_number": 2},
        ):
            with self.assertRaises(CredentialGrantRejected):
                self.authority.validate_use(
                    use["id"],
                    use_hash=use["content_hash"],
                    **changed,
                    **authority(),
                )

    def test_tables_are_append_only_and_store_no_secret_or_handle(self) -> None:
        grant = self.register()
        use = self.authorize()
        serialized = json.dumps(
            [grant, use], sort_keys=True, ensure_ascii=False
        ).lower()
        for forbidden in ("token", "cookie", "password", "secret", "127.0.0.1"):
            self.assertNotIn(forbidden, serialized)
        for statement in (
            "UPDATE credential_grants SET expires_at='x'",
            "DELETE FROM credential_use_receipts",
            "INSERT INTO credential_grants VALUES('forged','{}','x','x','x')",
        ):
            with self.subTest(statement=statement), self.assertRaises(sqlite3.DatabaseError):
                self.store.connection.execute(statement)
        self.assertEqual(
            self.store.connection.execute("PRAGMA integrity_check").fetchone()[0],
            "ok",
        )

    def test_serializable_handle_and_unsafe_refs_are_rejected(self) -> None:
        bad = grant_spec()
        bad["authority_ref"] = "api_key:secret-material"
        bad["content_hash"] = content_hash(
            {key: value for key, value in bad.items() if key != "content_hash"}
        )
        with self.assertRaises(CredentialGrantRejected):
            self.authority.register_grant(bad, idempotency_key="grant:unsafe")

        other_store = DaltonStore(":memory:")
        self.addCleanup(other_store.close)
        authority_store = CredentialAuthorityStore(
            other_store, handle_resolver=lambda grant: "plaintext-token", clock=self.clock
        )
        authority_store.register_grant(
            grant_spec(), idempotency_key="grant:serializable-handle"
        )
        use = authority_store.authorize_use(
            **use_authority(),
            **authority(),
            idempotency_key="use:serializable-handle",
        )
        with self.assertRaisesRegex(CredentialGrantRejected, "opaque host-owned"):
            authority_store.validate_use(
                use["id"],
                use_hash=use["content_hash"],
                **use_authority(),
                **authority(),
            )


if __name__ == "__main__":
    unittest.main()
