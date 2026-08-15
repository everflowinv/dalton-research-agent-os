from __future__ import annotations

import copy
import inspect
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from dalton_core.research_context import (
    build_claim_index,
    build_context_pack,
    build_fixture_runner_request,
    build_reference_fixture_plan,
)
from dalton_core.research_coordinator import (
    FixtureResearchCoordinator,
    RecordedShadowFixturePort,
    ResearchCoordinatorStore,
)
from dalton_core.research_verification import (
    CandidateStagingStore,
    InjectedStagingCrash,
    ResearchVerificationConflict,
    ResearchVerificationError,
    VerificationRejected,
    build_candidate_claim,
    build_candidate_evidence,
    build_source_verification_material,
    validate_candidate_claim,
    validate_source_verification_material,
    verify_numeric_spec,
    verify_source_material,
)
from dalton_core.store import content_hash
from tests.test_connector import assert_wire_schema


WHEN = datetime(2026, 8, 15, 8, 0, tzinfo=timezone.utc)
WIRE_WHEN = WHEN.isoformat(timespec="microseconds")


class FrozenClock:
    def __call__(self) -> datetime:
        return WHEN


class CrashAfterCommit:
    def __init__(self) -> None:
        self.triggered = False

    def __call__(self, stage, payload) -> None:
        if stage == "after_commit" and not self.triggered:
            self.triggered = True
            raise InjectedStagingCrash(payload["candidate_claim_ref"])


class CrashBeforeCommit:
    def __call__(self, stage, payload) -> None:
        if stage == "before_commit":
            raise InjectedStagingCrash(payload["candidate_claim_ref"])


class ResearchVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.task_ref = "work-order:verification-fixture:1"
        self.task_hash = content_hash({"work_order_ref": self.task_ref})
        self.plan = build_reference_fixture_plan(
            task_ref=self.task_ref, task_hash=self.task_hash, created_at=WIRE_WHEN
        )
        self.claim_index = build_claim_index(
            [],
            ledger_snapshot_ref="ledger-snapshot:verification:1",
            ledger_snapshot_hash=content_hash({"ledger_snapshot": "verification:1"}),
            created_at=WIRE_WHEN,
        )
        self.context = build_context_pack(
            [{
                "kind": "mandate", "ref": "mandate:verification:1",
                "hash": content_hash({"mandate": "verify fixtures"}),
                "priority": 100, "content": "Verify the recorded official sources.",
            }],
            task_ref=self.task_ref, task_hash=self.task_hash,
            compiled_plan_ref=self.plan["id"], compiled_plan_hash=self.plan["content_hash"],
            claim_index_ref=self.claim_index["id"], claim_index_hash=self.claim_index["content_hash"],
            created_at=WIRE_WHEN, max_tokens=100, max_bytes=2_000,
        )
        self.requests = {
            step["id"]: [
                build_fixture_runner_request(
                    self.plan, step, attempt_number=number, created_at=WIRE_WHEN
                )
                for number in range(1, step["max_attempts"] + 1)
            ]
            for step in self.plan["steps"]
        }
        self.coordinator_store = ResearchCoordinatorStore(":memory:")
        self.addCleanup(self.coordinator_store.close)
        self.port = RecordedShadowFixturePort(clock=FrozenClock())
        coordinator = FixtureResearchCoordinator(
            store=self.coordinator_store, connector_port=self.port, clock=FrozenClock()
        )
        result = coordinator.run(
            plan=self.plan, context_pack=self.context, claim_index=self.claim_index,
            runner_requests=self.requests, run_ref="research-run:verification:1",
            attempt_ref="research-attempt:verification:1",
            attempt_hash=content_hash({"attempt": "verification:1"}),
        )
        self.assertEqual(result["status"], "completed")
        self.checkpoint = self.coordinator_store.list_checkpoints(
            "research-run:verification:1"
        )[0]
        self.step = self.plan["steps"][0]
        self.request = self.requests[self.step["id"]][0]
        self.receipt = self.port.execute(
            self.step, self.request,
            idempotency_key=self.checkpoint["idempotency_key"],
        )
        self.material = build_source_verification_material(
            source_ref=self.step["source_ref"], scenario="success",
            source_envelope_ref=self.receipt["source_envelopes"][0]["ref"],
            source_envelope_hash=self.receipt["source_envelopes"][0]["hash"],
            artifact_ref=self.receipt["artifacts"][0]["ref"], created_at=WIRE_WHEN,
            retrieved_at=WIRE_WHEN,
            completeness="enumerated", status="complete",
        )
        self.source_bundle = verify_source_material(
            self.material, checkpoint=self.checkpoint, plan=self.plan,
            context_pack=self.context, step=self.step,
            runner_request=self.request, receipt=self.receipt,
        )
        self.spec = self.numeric_spec()
        self.numeric_bundle = verify_numeric_spec(
            self.spec, checkpoint_ref=self.checkpoint["id"],
            checkpoint_hash=self.checkpoint["content_hash"],
            source_material=self.material, source_bundle=self.source_bundle,
        )
        self.evidence = build_candidate_evidence(
            self.material, self.source_bundle,
            candidate_evidence_ref="candidate-evidence:cninfo-record-count:1",
            actor_ref="system:offline-verifier", created_at=WIRE_WHEN,
        )
        self.claim = build_candidate_claim(
            self.evidence, self.source_bundle, self.spec, self.numeric_bundle,
            candidate_claim_ref="candidate-claim:cninfo-record-count:1",
            subject_ref="company:600309", metric_or_aspect="announcement_count",
            basis="recorded-fixture", normalized_statement="The fixture contains one announcement.",
            actor_ref="system:offline-verifier", created_at=WIRE_WHEN,
        )
        self.assertEqual(self.claim["semantic_verification_status"], "unverified")

    def numeric_spec(self, **changes):
        base = {
            "schema_version": "0.1", "id": "numeric-spec:fixture:record-count:1",
            "created_at": WIRE_WHEN, "operator": "identity",
            "inputs": [{
                "name": "record_count", "value": "1", "unit": "records",
                "currency": None, "scale": "one", "period": "fixture:2026-08-15",
                "source_material_ref": self.material["id"],
                "source_material_hash": self.material["content_hash"],
                "json_pointer": "/0/announcements", "extractor": "count",
            }],
            "output_value": "1", "output_unit": "records", "output_currency": None,
            "output_scale": "one", "output_period": "fixture:2026-08-15",
            "rounding": {"mode": "half_up", "digits": 0},
        }
        base.update(changes)
        base["content_hash"] = content_hash(base)
        return base

    def authority_args(self):
        return {
            "checkpoint": self.checkpoint, "plan": self.plan,
            "context_pack": self.context, "step": self.step,
            "runner_request": self.request, "receipt": self.receipt,
        }

    def test_closed_wires_match_schemas_and_stage_is_candidate_only(self) -> None:
        self.assertEqual(self.source_bundle["verdict"], "pass")
        self.assertEqual(self.numeric_bundle["verdict"], "pass")
        for filename, wire in (
            ("source-verification-material.schema.json", self.material),
            ("numeric-verification-spec.schema.json", self.spec),
            ("verification-bundle.schema.json", self.source_bundle),
            ("verification-bundle.schema.json", self.numeric_bundle),
            ("candidate-evidence.schema.json", self.evidence),
            ("candidate-claim.schema.json", self.claim),
        ):
            assert_wire_schema(self, filename, wire)
        source = inspect.getsource(__import__(
            "dalton_core.research_verification", fromlist=["*"]
        ))
        self.assertNotRegex(source, r"from \.store import[^\n]*DaltonStore")
        store = CandidateStagingStore(":memory:")
        self.addCleanup(store.close)
        result = store.stage(
            **self.authority_args(),
            material=self.material, numeric_spec=self.spec,
            source_verification=self.source_bundle,
            numeric_verification=self.numeric_bundle, evidence=self.evidence,
            claim=self.claim, idempotency_key="stage:fixture:1",
        )
        self.assertEqual(result["write_status"], "fresh")
        self.assertEqual(store.counts()["candidate_claim_versions"], 1)
        tables = {row[0] for row in store.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        self.assertFalse({"evidence_versions", "claim_versions", "thesis_versions"} & tables)

    def test_source_replay_rejects_raw_source_and_cross_binding_drift(self) -> None:
        for field, value in (
            ("raw_payload_hash", "0" * 64),
            ("source_envelope_hash", "1" * 64),
            ("artifact_hash", "2" * 64),
        ):
            tampered = copy.deepcopy(self.material)
            tampered[field] = value
            tampered.pop("content_hash")
            tampered["content_hash"] = content_hash(tampered)
            if field == "raw_payload_hash":
                with self.assertRaises(ResearchVerificationConflict):
                    validate_source_verification_material(tampered)
            else:
                bundle = verify_source_material(
                    tampered, checkpoint=self.checkpoint, plan=self.plan,
                    context_pack=self.context, step=self.step,
                    runner_request=self.request, receipt=self.receipt,
                )
                self.assertEqual(bundle["verdict"], "reject")
        unknown = dict(self.material, secret="not-allowed")
        with self.assertRaises(ResearchVerificationError):
            validate_source_verification_material(unknown)

    def test_numeric_decimal_and_all_output_metadata_are_verified(self) -> None:
        variants = (
            {"output_value": "2"}, {"output_unit": "pages"},
            {"output_currency": "USD"}, {"output_scale": "thousand"},
            {"output_period": "fixture:other"},
        )
        for changes in variants:
            spec = self.numeric_spec(**changes)
            bundle = verify_numeric_spec(
                spec, checkpoint_ref=self.checkpoint["id"],
                checkpoint_hash=self.checkpoint["content_hash"],
                source_material=self.material, source_bundle=self.source_bundle,
            )
            self.assertEqual(bundle["verdict"], "reject", changes)
        bad_operator = self.numeric_spec(operator="multiply")
        with self.assertRaises(ResearchVerificationError):
            verify_numeric_spec(
                bad_operator, checkpoint_ref=self.checkpoint["id"],
                checkpoint_hash=self.checkpoint["content_hash"],
                source_material=self.material, source_bundle=self.source_bundle,
            )
        noncanonical = self.numeric_spec(output_value="1.0")
        with self.assertRaises(ResearchVerificationError):
            verify_numeric_spec(
                noncanonical, checkpoint_ref=self.checkpoint["id"],
                checkpoint_hash=self.checkpoint["content_hash"],
                source_material=self.material, source_bundle=self.source_bundle,
            )
        forged_inputs = copy.deepcopy(self.spec["inputs"])
        forged_inputs[0]["value"] = "2"
        forged = self.numeric_spec(inputs=forged_inputs, output_value="2")
        self.assertEqual(verify_numeric_spec(
            forged, checkpoint_ref=self.checkpoint["id"],
            checkpoint_hash=self.checkpoint["content_hash"],
            source_material=self.material, source_bundle=self.source_bundle,
        )["verdict"], "reject")

    def test_rounding_and_ratio_have_explicit_rules(self) -> None:
        inputs = [
            {"name":"a","value":"1","unit":"records","currency":None,"scale":"one","period":"p","source_material_ref":self.material["id"],"source_material_hash":self.material["content_hash"],"json_pointer":"/0/announcements","extractor":"count"},
            {"name":"b","value":"1","unit":"records","currency":None,"scale":"one","period":"p","source_material_ref":self.material["id"],"source_material_hash":self.material["content_hash"],"json_pointer":"/0/announcements","extractor":"count"},
        ]
        spec = self.numeric_spec(
            id="numeric-spec:ratio:1", operator="ratio", inputs=inputs,
            output_value="1", output_unit="ratio", output_currency=None,
            output_scale="one", output_period="p",
            rounding={"mode":"half_up","digits":2},
        )
        bundle = verify_numeric_spec(
            spec, checkpoint_ref=self.checkpoint["id"],
            checkpoint_hash=self.checkpoint["content_hash"], source_material=self.material, source_bundle=self.source_bundle,
        )
        self.assertEqual(bundle["verdict"], "pass")
        drift = self.numeric_spec(
            id="numeric-spec:ratio:2", operator="ratio", inputs=inputs,
            output_value="0.9", output_unit="ratio", output_currency=None,
            output_scale="one", output_period="p",
            rounding={"mode":"half_up","digits":2},
        )
        self.assertEqual(verify_numeric_spec(
            drift, checkpoint_ref=self.checkpoint["id"],
            checkpoint_hash=self.checkpoint["content_hash"], source_material=self.material, source_bundle=self.source_bundle,
        )["verdict"], "reject")

    def test_staging_rejects_failed_findings_and_candidate_drift(self) -> None:
        store = CandidateStagingStore(":memory:")
        self.addCleanup(store.close)
        failed_spec = self.numeric_spec(output_value="2")
        failed_bundle = verify_numeric_spec(
            failed_spec, checkpoint_ref=self.checkpoint["id"],
            checkpoint_hash=self.checkpoint["content_hash"], source_material=self.material, source_bundle=self.source_bundle,
        )
        with self.assertRaises(VerificationRejected):
            store.stage(
                **self.authority_args(),
                material=self.material, numeric_spec=failed_spec,
                source_verification=self.source_bundle,
                numeric_verification=failed_bundle, evidence=self.evidence,
                claim=self.claim, idempotency_key="stage:failed",
            )
        forged_source = copy.deepcopy(self.source_bundle)
        forged_source["findings"][0]["message"] = "caller forged this pass bundle"
        forged_source["findings"][0].pop("content_hash")
        forged_source["findings"][0]["content_hash"] = content_hash(
            forged_source["findings"][0]
        )
        forged_source.pop("content_hash")
        forged_source["content_hash"] = content_hash(forged_source)
        with self.assertRaises(ResearchVerificationConflict):
            store.stage(
                **self.authority_args(), material=self.material,
                numeric_spec=self.spec, source_verification=forged_source,
                numeric_verification=self.numeric_bundle, evidence=self.evidence,
                claim=self.claim, idempotency_key="stage:forged-verification",
            )
        drifted = copy.deepcopy(self.claim)
        drifted["normalized_statement"] = "A different candidate statement."
        drifted["value"] = "2"
        drifted.pop("content_hash")
        drifted["content_hash"] = content_hash(drifted)
        with self.assertRaises(ResearchVerificationConflict):
            store.stage(
                **self.authority_args(),
                material=self.material, numeric_spec=self.spec,
                source_verification=self.source_bundle,
                numeric_verification=self.numeric_bundle, evidence=self.evidence,
                claim=drifted, idempotency_key="stage:drift",
            )
        mislabeled_evidence = copy.deepcopy(self.evidence)
        mislabeled_evidence["source_type"] = "live_authority"
        mislabeled_evidence["independence_group"] = "independence:unrelated"
        mislabeled_evidence.pop("content_hash")
        mislabeled_evidence["content_hash"] = content_hash(mislabeled_evidence)
        mislabeled_claim = build_candidate_claim(
            mislabeled_evidence, self.source_bundle, self.spec, self.numeric_bundle,
            candidate_claim_ref="candidate-claim:mislabeled:1",
            subject_ref="company:600309", metric_or_aspect="announcement_count",
            basis="recorded-fixture", normalized_statement="One recorded announcement.",
            actor_ref="system:offline-verifier", created_at=WIRE_WHEN,
        )
        with self.assertRaises(ResearchVerificationConflict):
            store.stage(
                **self.authority_args(), material=self.material,
                numeric_spec=self.spec, source_verification=self.source_bundle,
                numeric_verification=self.numeric_bundle,
                evidence=mislabeled_evidence, claim=mislabeled_claim,
                idempotency_key="stage:mislabeled-evidence",
            )
        semantically_overclaimed = copy.deepcopy(self.claim)
        semantically_overclaimed["semantic_verification_status"] = "verified"
        semantically_overclaimed.pop("content_hash")
        semantically_overclaimed["content_hash"] = content_hash(semantically_overclaimed)
        with self.assertRaises(ResearchVerificationError):
            validate_candidate_claim(semantically_overclaimed)
        self.assertTrue(all(value == 0 for value in store.counts().values()))

    def test_staging_is_idempotent_and_recovers_after_commit_response_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "candidate.db"
            store = CandidateStagingStore(path, fault_hook=CrashAfterCommit())
            with self.assertRaises(InjectedStagingCrash):
                store.stage(
                    **self.authority_args(),
                    material=self.material, numeric_spec=self.spec,
                    source_verification=self.source_bundle,
                    numeric_verification=self.numeric_bundle, evidence=self.evidence,
                    claim=self.claim, idempotency_key="stage:crash",
                )
            store.close()
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            recovered = CandidateStagingStore(path)
            self.addCleanup(recovered.close)
            duplicate = recovered.stage(
                **self.authority_args(),
                material=self.material, numeric_spec=self.spec,
                source_verification=self.source_bundle,
                numeric_verification=self.numeric_bundle, evidence=self.evidence,
                claim=self.claim, idempotency_key="stage:crash",
            )
            self.assertEqual(duplicate["write_status"], "duplicate")
            changed = copy.deepcopy(self.claim)
            changed["normalized_statement"] += " Reviewed."
            changed.pop("content_hash")
            changed["content_hash"] = content_hash(changed)
            with self.assertRaises(ResearchVerificationConflict):
                recovered.stage(
                    **self.authority_args(),
                    material=self.material, numeric_spec=self.spec,
                    source_verification=self.source_bundle,
                    numeric_verification=self.numeric_bundle, evidence=self.evidence,
                    claim=changed, idempotency_key="stage:crash",
                )
            self.assertEqual(recovered.counts()["candidate_claim_versions"], 1)

    def test_staging_rolls_back_an_in_transaction_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "candidate-rollback.db"
            store = CandidateStagingStore(path, fault_hook=CrashBeforeCommit())
            with self.assertRaises(InjectedStagingCrash):
                store.stage(
                    **self.authority_args(), material=self.material,
                    numeric_spec=self.spec, source_verification=self.source_bundle,
                    numeric_verification=self.numeric_bundle, evidence=self.evidence,
                    claim=self.claim, idempotency_key="stage:rollback",
                )
            self.assertTrue(all(value == 0 for value in store.counts().values()))
            store.close()
            retry = CandidateStagingStore(path)
            self.addCleanup(retry.close)
            result = retry.stage(
                **self.authority_args(), material=self.material,
                numeric_spec=self.spec, source_verification=self.source_bundle,
                numeric_verification=self.numeric_bundle, evidence=self.evidence,
                claim=self.claim, idempotency_key="stage:rollback",
            )
            self.assertEqual(result["write_status"], "fresh")


if __name__ == "__main__":
    unittest.main()
