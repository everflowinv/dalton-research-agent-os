from __future__ import annotations

import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dalton_core.capability_catalog import (
    AuthorizationRequired,
    ApprovalRequired,
    CapabilityCatalog,
    CapabilityDescriptor,
    CapabilityLease,
    CatalogValidationError,
    GovernancePolicyRejected,
    LeaseNotUsable,
    PermissionDenied,
    StaleCatalog,
    canonical_hash,
)
from dalton_core.contracts import WorkOrder


H_SOURCE = "1" * 64
H_SCHEMA = "2" * 64
NOW = datetime(2026, 8, 14, 6, 0, tzinfo=timezone.utc)


def allowed_permissions() -> dict:
    return {
        "risk_class": "low",
        "network": False,
        "filesystem_read": ["workspace:staging-input"],
        "filesystem_write": ["workspace:staging-output"],
        "credential_slot_refs": [],
        "core_db": False,
        "side_effects": ["write:staging-output"],
    }


class MutableClock:
    def __init__(self) -> None:
        self.value = NOW

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


class FakeAuthorities:
    """Trusted resolver fixtures; callers never supply these receipts."""

    def __init__(self) -> None:
        self.approved = True
        self.policy_permissions = allowed_permissions()
        self.policy_generation = 1

    def approval(self, query: dict) -> dict | None:
        if not self.approved:
            return None
        suffix = canonical_hash(
            {"capability_id": query["capability_id"], "source_hash": query["source_hash"]}
        )[:16]
        wire = {
            "schema_version": "0.1",
            "approval_ref": f"approval:{suffix}",
            "capability_id": query["capability_id"],
            "registry_revision_ref": f"capability-version:{suffix}",
            "artifact_ref": query["source_ref"],
            "artifact_hash": query["source_hash"],
            "schema_hash": query["schema_hash"],
            "fixture_manifest_hash": "4" * 64,
            "attestation_ref": f"attestation:{suffix}",
            "attestation_hash": "5" * 64,
            "decision_ref": f"capability-decision:{suffix}",
            "decision": "approve",
            "approved_by": "human:lumos",
            "approved_permissions": copy.deepcopy(query["requested_permissions"]),
            "active": True,
            "effective_from": "2026-08-14T05:00:00+00:00",
            "effective_until": None,
        }
        wire["receipt_hash"] = canonical_hash(wire)
        return wire

    def policy(self, query: dict) -> dict:
        wire = {
            "schema_version": "0.1",
            "policy_ref": query["policy_ref"],
            "effective_from": "2026-08-14T05:00:00+00:00",
            "effective_until": None,
            "allowed_principal_refs": ["principal:worker-1"],
            "allowed_permissions": copy.deepcopy(self.policy_permissions),
            "max_lease_seconds": 120,
        }
        # generation is deliberately authority content so tests can simulate a
        # policy replacement without letting the caller choose its hash.
        if self.policy_generation != 1:
            wire["max_lease_seconds"] = 119
        wire["content_hash"] = canonical_hash(wire)
        return wire


def descriptor_spec(
    capability_id: str = "capability:dalton:research:formatter",
    *,
    version: int = 1,
    name: str = "record-formatter",
    state: str = "ready",
    side_effects: list[str] | None = None,
    visibility: list[str] | None = None,
) -> dict:
    source_type, namespace = capability_id.split(":")[1:3]
    return {
        "schema_version": "0.1",
        "id": capability_id,
        "version": version,
        "created_at": "2026-08-14T06:00:00+00:00",
        "kind": "transform",
        "name": name,
        "label": "Canonical record formatter",
        "summary": "Format vendor records into deterministic canonical JSONL data",
        "aliases": ["data formatter", "normalize records"],
        "tags": ["formatter", "jsonl", "deterministic"],
        "intent_examples": ["format vendor records", "canonicalize tabular records"],
        "source": {
            "type": source_type,
            "namespace": namespace,
            "source_ref": "artifact:formatter-source",
            "source_version": str(version),
        },
        "contract": {
            "mode": "process",
            "input_schema_ref": "schema:formatter-input-v1",
            "output_schema_ref": "schema:formatter-output-v1",
            "instruction_ref": None,
            "adapter_ref": "adapter:dalton-process-v1",
        },
        "permissions": {
            "risk_class": "low",
            "network": False,
            "filesystem_read": ["workspace:staging-input"],
            "filesystem_write": ["workspace:staging-output"],
            "credential_slot_refs": [],
            "core_db": False,
            "side_effects": side_effects or ["write:staging-output"],
        },
        "eligibility": {
            "state": state,
            "visibility_scopes": visibility or ["research"],
            "policy_ref": "policy:capability-v1",
            "valid_until": None,
        },
        "source_hash": H_SOURCE,
        "schema_hash": H_SCHEMA,
    }


def work_order(capability_id: str, effects: tuple[str, ...] = ("write:staging-output",)) -> WorkOrder:
    return WorkOrder(
        schema_version="0.1",
        id="work:formatter-1",
        created_at="2026-08-14T06:00:00+00:00",
        updated_at="2026-08-14T06:00:00+00:00",
        question="Format the vendor records",
        requested_capabilities=(capability_id,),
        runtime_profile_ref="runtime:process-v1",
        budget={"max_seconds": 10},
        idempotency_key="formatter-1",
        declared_side_effects=effects,
        status="ready",
    )


class CapabilityCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = MutableClock()
        self.authorities = FakeAuthorities()
        self.catalog = CapabilityCatalog(
            clock=self.clock,
            approval_resolver=self.authorities.approval,
            policy_resolver=self.authorities.policy,
        )

    def tearDown(self) -> None:
        self.catalog.close()

    def _prepare(self, descriptor, **overrides):
        args = {
            "capability_id": descriptor.id,
            "revision_ref": descriptor.revision_ref,
            "catalog_epoch": self.catalog.epoch,
            "descriptor_hash": descriptor.content_hash,
            "source_hash": descriptor.source_hash,
            "schema_hash": descriptor.schema_hash,
            "policy_ref": descriptor.eligibility.policy_ref,
            "policy_hash": self.authorities.policy(
                {
                    "policy_ref": descriptor.eligibility.policy_ref,
                    "principal_ref": "principal:worker-1",
                    "at": NOW.isoformat(),
                }
            )["content_hash"],
            "principal_ref": "principal:worker-1",
            "visibility_scopes": ["research"],
            "ttl_seconds": 45,
        }
        args.update(overrides)
        return self.catalog.prepare(work_order(descriptor.id), **args)

    def test_search_describe_prepare_formatter_positive_path(self) -> None:
        # Distractors make the top-five assertion meaningful.
        for index in range(6):
            spec = descriptor_spec(
                f"capability:tool:general:unrelated-{index}",
                name=f"unrelated-{index}",
                side_effects=[],
            )
            spec["summary"] = "Send notification and inspect calendars"
            spec["aliases"] = ["calendar helper"]
            spec["tags"] = ["calendar"]
            spec["intent_examples"] = ["schedule a meeting"]
            self.catalog.publish(spec)
        formatter = self.catalog.publish(descriptor_spec())

        hits = self.catalog.search(
            "format vendor records canonical JSONL", visibility_scopes=["research"], limit=5
        )
        self.assertIn(formatter.id, [hit.id for hit in hits])
        self.assertEqual(formatter.id, hits[0].id)
        described = self.catalog.describe(
            formatter.id, visibility_scopes=["research"], catalog_epoch=self.catalog.epoch
        )
        self.assertEqual(formatter, described)

        lease = self._prepare(formatter)
        self.assertEqual(formatter.revision_ref, lease.revision_ref)
        self.assertEqual(work_order(formatter.id).id, lease.work_order_ref)
        self.assertEqual(("write:staging-output",), lease.allowed_side_effects)
        self.assertEqual("lease:" + lease.content_hash, lease.id)
        self.assertEqual(lease, CapabilityLease.from_dict(lease.to_dict()))
        self.assertEqual(formatter, CapabilityDescriptor.from_dict(formatter.to_dict()))
        self.assertEqual(formatter.source_hash, formatter.approval.artifact_hash)
        self.assertEqual(H_SCHEMA, formatter.approval.schema_hash)
        contracts = Path(__file__).resolve().parents[1] / "contracts"
        descriptor_schema = json.loads(
            (contracts / "capability-descriptor.schema.json").read_text(encoding="utf-8")
        )
        lease_schema = json.loads(
            (contracts / "capability-lease.schema.json").read_text(encoding="utf-8")
        )
        self.assertEqual(set(formatter.to_dict()), set(descriptor_schema["required"]))
        self.assertEqual(set(lease.to_dict()), set(lease_schema["required"]))
        self.assertEqual(
            lease,
            self.catalog.validate_lease_for_use(
                lease.id,
                work_order=work_order(formatter.id),
                principal_ref="principal:worker-1",
                permissions=lease.permissions.to_dict(),
                side_effects=lease.allowed_side_effects,
            ),
        )
        self.assertFalse(hasattr(self.catalog, "execute"))

    def test_prompt_directory_is_compact_and_has_no_full_contract(self) -> None:
        self.catalog.publish(descriptor_spec())
        hits = self.catalog.search("formatter", visibility_scopes=["research"])
        prompt = self.catalog.render_prompt_directory(hits)
        self.assertIn("record-formatter", prompt)
        for forbidden in (
            "input_schema_ref", "output_schema_ref", "instruction_ref", "adapter_ref",
            "credential_slot_refs", "source_hash", "schema_hash", '"properties"',
        ):
            self.assertNotIn(forbidden, prompt)
        self.assertLess(len(prompt), 600)

    def test_stale_epoch_revision_and_hashes_fail_closed(self) -> None:
        formatter = self.catalog.publish(descriptor_spec())
        old_epoch = self.catalog.epoch
        self.catalog.publish(descriptor_spec("capability:tool:general:clock", name="clock"))
        with self.assertRaises(StaleCatalog):
            self._prepare(formatter, catalog_epoch=old_epoch)

        current = self.catalog.describe(formatter.id, visibility_scopes=["research"])
        for field in ("descriptor_hash", "source_hash", "schema_hash", "policy_ref"):
            bad = "0" * 64 if field.endswith("hash") else "policy:wrong"
            with self.subTest(field=field), self.assertRaises(StaleCatalog):
                self._prepare(current, **{field: bad})

        update = descriptor_spec(version=2)
        update["summary"] = "Updated deterministic canonical JSONL formatter"
        latest = self.catalog.publish(update)
        with self.assertRaises(StaleCatalog):
            self._prepare(latest, revision_ref=current.revision_ref)

    def test_auth_visibility_and_expiry_are_not_eligible(self) -> None:
        auth = self.catalog.publish(descriptor_spec(state="auth_required"))
        hits = self.catalog.search("formatter", visibility_scopes=["research"])
        self.assertEqual("auth_required", hits[0].state)
        with self.assertRaises(AuthorizationRequired):
            self._prepare(auth)
        with self.assertRaises(Exception):
            self.catalog.describe(auth.id, visibility_scopes=["public"])

        hidden_spec = descriptor_spec("capability:tool:private:hidden", name="hidden", visibility=["private"])
        self.catalog.publish(hidden_spec)
        self.assertEqual([], self.catalog.search("hidden", visibility_scopes=["research"]))

    def test_prepare_rejects_side_effect_or_work_order_escalation(self) -> None:
        formatter = self.catalog.publish(descriptor_spec())
        args = {
            "capability_id": formatter.id,
            "revision_ref": formatter.revision_ref,
            "catalog_epoch": self.catalog.epoch,
            "descriptor_hash": formatter.content_hash,
            "source_hash": formatter.source_hash,
            "schema_hash": formatter.schema_hash,
            "policy_ref": formatter.eligibility.policy_ref,
            "policy_hash": self.authorities.policy(
                {
                    "policy_ref": formatter.eligibility.policy_ref,
                    "principal_ref": "principal:worker-1",
                    "at": NOW.isoformat(),
                }
            )["content_hash"],
            "principal_ref": "principal:worker-1",
            "visibility_scopes": ["research"],
        }
        with self.assertRaises(PermissionDenied):
            self.catalog.prepare(work_order(formatter.id, ("network:send",)), **args)
        with self.assertRaises(PermissionDenied):
            self.catalog.prepare(work_order("capability:tool:general:other"), **args)
        with self.assertRaises(CatalogValidationError):
            self._prepare(formatter, ttl_seconds=301)

    def test_publish_requires_current_human_approval_receipt(self) -> None:
        without_resolver = CapabilityCatalog(
            clock=self.clock, policy_resolver=self.authorities.policy
        )
        try:
            with self.assertRaises(ApprovalRequired):
                without_resolver.publish(descriptor_spec())
        finally:
            without_resolver.close()
        self.authorities.approved = False
        with self.assertRaises(ApprovalRequired):
            self.catalog.publish(descriptor_spec())

        self.authorities.approved = True
        original = self.authorities.approval

        def wrong_artifact(query):
            receipt = original(query)
            assert receipt is not None
            receipt["artifact_hash"] = "9" * 64
            receipt["receipt_hash"] = canonical_hash(
                {key: value for key, value in receipt.items() if key != "receipt_hash"}
            )
            return receipt

        self.catalog.approval_resolver = wrong_artifact
        with self.assertRaises(ApprovalRequired):
            self.catalog.publish(descriptor_spec())

        def wrong_permissions(query):
            receipt = original(query)
            assert receipt is not None
            receipt["approved_permissions"] = allowed_permissions()
            receipt["receipt_hash"] = canonical_hash(
                {key: value for key, value in receipt.items() if key != "receipt_hash"}
            )
            return receipt

        self.catalog.approval_resolver = wrong_permissions
        escalated = descriptor_spec()
        escalated["permissions"]["network"] = True
        with self.assertRaises(ApprovalRequired):
            self.catalog.publish(escalated)

    def test_policy_authority_rejects_fake_hash_and_permission_escalation(self) -> None:
        formatter = self.catalog.publish(descriptor_spec())
        with self.assertRaises(GovernancePolicyRejected):
            self._prepare(formatter, policy_hash="f" * 64)

        hostile = descriptor_spec(
            "capability:tool:hostile:root-access", name="root-access", side_effects=[]
        )
        hostile["permissions"] = {
            "risk_class": "high",
            "network": True,
            "filesystem_read": ["/"],
            "filesystem_write": ["/"],
            "credential_slot_refs": ["credential-slot:admin"],
            "core_db": True,
            "side_effects": ["write:staging-output"],
        }
        descriptor = self.catalog.publish(hostile)
        with self.assertRaises(GovernancePolicyRejected):
            self._prepare(descriptor)

    def test_use_time_gate_rejects_wrong_identity_work_permissions_and_effects(self) -> None:
        formatter = self.catalog.publish(descriptor_spec())
        lease = self._prepare(formatter)
        common = {
            "work_order": work_order(formatter.id),
            "principal_ref": "principal:worker-1",
            "permissions": lease.permissions.to_dict(),
            "side_effects": lease.allowed_side_effects,
        }
        with self.assertRaises(LeaseNotUsable):
            self.catalog.validate_lease_for_use(
                lease.id, **{**common, "principal_ref": "principal:other"}
            )
        other_work = work_order(formatter.id)
        other_work = WorkOrder.from_dict(
            {**other_work.to_dict(), "id": "work:other", "idempotency_key": "other"}
        )
        with self.assertRaises(LeaseNotUsable):
            self.catalog.validate_lease_for_use(
                lease.id, **{**common, "work_order": other_work}
            )
        changed_permissions = lease.permissions.to_dict()
        changed_permissions["network"] = True
        with self.assertRaises(LeaseNotUsable):
            self.catalog.validate_lease_for_use(
                lease.id, **{**common, "permissions": changed_permissions}
            )
        with self.assertRaises(LeaseNotUsable):
            self.catalog.validate_lease_for_use(
                lease.id, **{**common, "side_effects": ["network:send"]}
            )

    def test_use_time_gate_rejects_expired_epoch_policy_and_approval_changes(self) -> None:
        formatter = self.catalog.publish(descriptor_spec())
        lease = self._prepare(formatter, ttl_seconds=45)
        args = {
            "work_order": work_order(formatter.id),
            "principal_ref": "principal:worker-1",
            "permissions": lease.permissions.to_dict(),
            "side_effects": lease.allowed_side_effects,
        }
        self.authorities.policy_generation = 2
        with self.assertRaises(LeaseNotUsable):
            self.catalog.validate_lease_for_use(lease.id, **args)
        self.authorities.policy_generation = 1
        self.authorities.approved = False
        with self.assertRaises(ApprovalRequired):
            self.catalog.validate_lease_for_use(lease.id, **args)
        self.authorities.approved = True
        self.catalog.publish(
            descriptor_spec("capability:tool:general:epoch-bump", name="epoch-bump", side_effects=[])
        )
        with self.assertRaises(LeaseNotUsable):
            self.catalog.validate_lease_for_use(lease.id, **args)

        current = self.catalog.describe(formatter.id, visibility_scopes=["research"])
        fresh = self._prepare(current, ttl_seconds=1)
        self.clock.advance(2)
        with self.assertRaises(LeaseNotUsable):
            self.catalog.validate_lease_for_use(
                fresh.id,
                work_order=work_order(current.id),
                principal_ref="principal:worker-1",
                permissions=fresh.permissions.to_dict(),
                side_effects=fresh.allowed_side_effects,
            )
        # Historical reads intentionally remain available for audit.
        self.assertEqual(fresh.id, self.catalog.get_lease(fresh.id).id)

    def test_namespaced_ids_make_duplicate_display_names_unambiguous(self) -> None:
        first = self.catalog.publish(descriptor_spec(name="formatter"))
        second = self.catalog.publish(
            descriptor_spec("capability:tool:vendor:formatter", name="formatter")
        )
        hits = self.catalog.search("formatter", visibility_scopes=["research"])
        self.assertEqual({first.id, second.id}, {hit.id for hit in hits})
        self.assertEqual(first.id, self.catalog.describe(first.id, visibility_scopes=["research"]).id)
        self.assertEqual(second.id, self.catalog.describe(second.id, visibility_scopes=["research"]).id)
        with self.assertRaises(CatalogValidationError):
            self.catalog.describe("formatter", visibility_scopes=["research"])

    def test_closed_descriptor_never_accepts_secret_or_body(self) -> None:
        spec = descriptor_spec()
        for hostile_key in ("token", "credential", "skill_body", "schema_body", "execute"):
            hostile = copy.deepcopy(spec)
            hostile[hostile_key] = "secret"
            with self.subTest(hostile_key=hostile_key), self.assertRaises(CatalogValidationError):
                self.catalog.publish(hostile)
        hostile = descriptor_spec()
        hostile["permissions"]["credential_slot_refs"] = ["Bearer secret-value"]
        with self.assertRaises(CatalogValidationError):
            self.catalog.publish(hostile)

        descriptor = self.catalog.publish(descriptor_spec())
        expected = canonical_hash(descriptor.to_dict(include_hash=False))
        self.assertEqual(expected, descriptor.content_hash)
        tampered = descriptor.to_dict()
        tampered["summary"] = "tampered"
        with self.assertRaises(CatalogValidationError):
            CapabilityDescriptor.from_dict(tampered)


if __name__ == "__main__":
    unittest.main()
