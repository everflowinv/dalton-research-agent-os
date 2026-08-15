from __future__ import annotations

import copy
import json
import sqlite3
import unittest
from unittest.mock import patch

from dalton_core.authority_resolver import AuthorityResolutionConflict
from dalton_core.connector_runner import validate_connector_runner_request
from dalton_core.research_coordinator import validate_connector_completion_receipt
from dalton_core.research_verification import (
    CandidateStagingStore,
    VerificationRejected,
    build_authority_source_material,
    build_candidate_claim,
    build_candidate_evidence,
    verify_authority_source_material,
    verify_numeric_spec,
)
from dalton_core.sec_authority_harness import SecAuthorityHarness, WIRE_WHEN
from dalton_core.store import content_hash
from tests.test_connector_runner import assert_wire_schema


class AuthorityResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = SecAuthorityHarness()
        self.addCleanup(self.harness.close)

    def receipt(self) -> dict:
        row = self.harness.coordinator_store.connection.execute(
            "SELECT receipt_json FROM research_completion_receipts"
        ).fetchone()
        self.assertIsNotNone(row)
        return json.loads(row[0])

    def resolve(self):
        resolver = self.harness.resolver()
        source_ref = self.harness.checkpoint["source_envelopes"][0]["ref"]
        return resolver, resolver.resolve(
            source_ref, checkpoint_ref=self.harness.checkpoint["id"]
        )

    def numeric_spec(self, material: dict, *, value: str = "3") -> dict:
        spec = {
            "schema_version": "0.1",
            "id": "numeric-spec:sec:filing-count:1",
            "created_at": WIRE_WHEN,
            "operator": "identity",
            "inputs": [{
                "name": "filing_count",
                "value": value,
                "unit": "records",
                "currency": None,
                "scale": "one",
                "period": "FY2025",
                "source_material_ref": material["id"],
                "source_material_hash": material["content_hash"],
                "json_pointer": "/records",
                "extractor": "count",
            }],
            "output_value": value,
            "output_unit": "records",
            "output_currency": None,
            "output_scale": "one",
            "output_period": "FY2025",
            "rounding": {"mode": "down", "digits": 0},
        }
        spec["content_hash"] = content_hash(spec)
        return spec

    def source_bundle(self, material: dict, resolver) -> dict:
        return verify_authority_source_material(
            material,
            resolver=resolver,
            checkpoint=self.harness.checkpoint,
            plan=self.harness.plan,
            context_pack=self.harness.context,
            step=self.harness.step,
            runner_request=self.harness.coordinator_request,
            receipt=self.receipt(),
        )

    def test_sec_authority_resolves_and_stages_numeric_candidate(self) -> None:
        authority_connections = [
            self.harness.core.connection,
            self.harness.scheduler.connection,
            self.harness.coordinator_store.connection,
        ]
        writes_before = [connection.total_changes for connection in authority_connections]
        resolver, resolved = self.resolve()
        self.assertEqual(
            [connection.total_changes for connection in authority_connections],
            writes_before,
        )
        self.assertEqual(resolved.summary["source_record_refs"], [
            "sec:filing:0000000002-25-000002",
            "sec:filing:0000000003-25-000003",
            "sec:filing:0000000004-25-000004",
        ])
        self.assertEqual(
            resolved.records["result_envelope"]["actual_side_effects"],
            ["read:public-http"],
        )
        self.assertNotEqual(
            resolved.summary["runner_request_ref"],
            resolved.summary["actual_runner_request_ref"],
        )
        assert_wire_schema(self, "authority-resolution.schema.json", resolved.summary)

        material = build_authority_source_material(resolved)
        assert_wire_schema(
            self, "authority-source-verification-material.schema.json", material
        )
        source_bundle = self.source_bundle(material, resolver)
        self.assertEqual(source_bundle["verdict"], "pass")

        spec = self.numeric_spec(material)
        numeric = verify_numeric_spec(
            spec,
            checkpoint_ref=self.harness.checkpoint["id"],
            checkpoint_hash=self.harness.checkpoint["content_hash"],
            source_material=material,
            source_bundle=source_bundle,
        )
        self.assertEqual(numeric["verdict"], "pass")
        evidence = build_candidate_evidence(
            material,
            source_bundle,
            candidate_evidence_ref="candidate-evidence:sec:1",
            actor_ref="system:offline-verifier",
            created_at=WIRE_WHEN,
            verification_mode="connector_authority",
        )
        self.assertEqual(evidence["source_type"], "official_filing")
        claim = build_candidate_claim(
            evidence,
            source_bundle,
            spec,
            numeric,
            candidate_claim_ref="candidate-claim:sec:1",
            subject_ref="company:issuer-0000789019",
            metric_or_aspect="filing_count",
            basis="official-filing",
            normalized_statement=(
                "The bounded SEC result contains three 2025 10-Q filings."
            ),
            actor_ref="system:offline-verifier",
            created_at=WIRE_WHEN,
        )
        self.assertEqual(claim["semantic_verification_status"], "unverified")

        staging = CandidateStagingStore(":memory:")
        self.addCleanup(staging.close)
        arguments = {
            "checkpoint": self.harness.checkpoint,
            "plan": self.harness.plan,
            "context_pack": self.harness.context,
            "step": self.harness.step,
            "runner_request": self.harness.coordinator_request,
            "receipt": self.receipt(),
            "material": material,
            "numeric_spec": spec,
            "source_verification": source_bundle,
            "numeric_verification": numeric,
            "evidence": evidence,
            "claim": claim,
            "idempotency_key": "stage:sec:1",
            "verification_mode": "connector_authority",
            "authority_resolver": resolver,
        }
        self.assertEqual(staging.stage(**arguments)["write_status"], "fresh")
        self.assertEqual(staging.stage(**arguments)["write_status"], "duplicate")
        self.assertEqual(staging.counts()["candidate_claim_versions"], 1)

    def test_raw_source_type_and_actual_request_rebinding_fail_closed(self) -> None:
        resolver, resolved = self.resolve()
        material = build_authority_source_material(resolved)

        raw_hash = resolved.summary["raw_response_hash"]
        raw_path = self.harness.spool._objects / raw_hash[:2] / raw_hash
        original = raw_path.read_bytes()
        raw_path.write_bytes(original + b" ")
        try:
            with self.assertRaises(AuthorityResolutionConflict):
                resolver.resolve(
                    resolved.summary["source_envelope_ref"],
                    checkpoint_ref=self.harness.checkpoint["id"],
                )
        finally:
            raw_path.write_bytes(original)

        rebound_material = copy.deepcopy(material)
        rebound_material["source_type"] = "public_web"
        rebound_material["content_hash"] = content_hash({
            key: value
            for key, value in rebound_material.items()
            if key != "content_hash"
        })
        rejected = self.source_bundle(rebound_material, resolver)
        self.assertEqual(rejected["verdict"], "reject")
        with self.assertRaises(VerificationRejected):
            build_candidate_evidence(
                rebound_material,
                rejected,
                candidate_evidence_ref="candidate-evidence:rebound",
                actor_ref="system:offline-verifier",
                created_at=WIRE_WHEN,
                verification_mode="connector_authority",
            )

        original_request = resolver.runner_journal.request

        def rebound_request(ref: str) -> dict:
            wire = original_request(ref)
            wire["principal_ref"] = "principal:rebound"
            wire["content_hash"] = content_hash({
                key: value for key, value in wire.items() if key != "content_hash"
            })
            return validate_connector_runner_request(wire)

        with patch.object(
            resolver.runner_journal, "request", side_effect=rebound_request
        ), self.assertRaises(AuthorityResolutionConflict):
            resolver.resolve(
                resolved.summary["source_envelope_ref"],
                checkpoint_ref=self.harness.checkpoint["id"],
            )

    def test_v0_1_receipt_is_compatible_but_not_live_authority(self) -> None:
        resolver, resolved = self.resolve()
        receipt = self.receipt()
        receipt["schema_version"] = "0.1"
        receipt.pop("actual_runner_request_ref")
        receipt.pop("actual_runner_request_hash")
        receipt["content_hash"] = content_hash({
            key: value for key, value in receipt.items() if key != "content_hash"
        })
        self.assertEqual(
            validate_connector_completion_receipt(receipt)["schema_version"], "0.1"
        )

        checkpoint, plan, context, step, request, _old, checkpoints = (
            resolver._checkpoint_records(self.harness.checkpoint["id"])
        )
        changed_checkpoint = dict(checkpoint)
        changed_checkpoint["completion_receipt_hash"] = receipt["content_hash"]
        changed_checkpoint["content_hash"] = content_hash({
            key: value
            for key, value in changed_checkpoint.items()
            if key != "content_hash"
        })
        with patch.object(
            resolver,
            "_checkpoint_records",
            return_value=(
                changed_checkpoint, plan, context, step, request, receipt, checkpoints
            ),
        ), self.assertRaises(AuthorityResolutionConflict):
            resolver.resolve(
                resolved.summary["source_envelope_ref"],
                checkpoint_ref=self.harness.checkpoint["id"],
            )

    def test_coordinator_request_and_receipt_authority_is_immutable(self) -> None:
        connection = self.harness.coordinator_store.connection
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE research_runner_requests SET created_at=created_at"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM research_completion_receipts")


if __name__ == "__main__":
    unittest.main()
