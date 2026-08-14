import json
import sqlite3
import unittest
from pathlib import Path

from dalton_core.observability import (
    ObservabilityConflict,
    ObservabilityNotFound,
    ObservabilityStore,
    ObservabilityValidationError,
)
from dalton_core.store import DaltonStore, content_hash


ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "contracts"
WHEN = "2026-01-01T00:00:00+00:00"


def invocation(identifier="inv-1", *, provider="provider-a", model="model-a"):
    return {
        "schema_version": "0.1",
        "id": identifier,
        "created_at": WHEN,
        "work_order_ref": "work-1",
        "profile_ref": "profile-1",
        "granularity": "task",
        "capability": "research",
        "provider": provider,
        "model": model,
        "model_family": "family-a",
        "input_refs": [],
        "output_refs": ["artifact:report", "artifact:inline"],
        "started_at": WHEN,
        "completed_at": WHEN,
        "usage": {"tokens": 3},
        "side_effects": [],
        "runtime_ref": "runtime:test",
        "actor_ref": "agent:test",
        "parent_ref": None,
        "environment_hash": "e" * 64,
    }


class ObservabilityTests(unittest.TestCase):
    def setUp(self):
        self.store = DaltonStore(":memory:")
        self.store.register_invocation(invocation())
        self.obs = ObservabilityStore(self.store)
        self.addCleanup(self.store.close)

    def workflow(self, workflow_ref="workflow-1", version_id="workflow-v1", roots=("root",)):
        return self.obs.create_workflow_version(
            workflow_ref,
            version_id=version_id,
            title="Research Wanhua",
            objective="Update the thesis",
            scope_refs=["entity:wanhua"],
            root_work_order_refs=list(roots),
            governance_policy_ref="policy-1",
            actor_ref="human:lumos",
        )

    def usage(self, entry_id="usage-1", **overrides):
        args = {
            "entry_id": entry_id,
            "occurred_at": WHEN,
            "metering_source": "provider_reported",
            "measurement_status": "final",
            "raw_usage": {"input_tokens": 2, "output_tokens": 1},
            "workflow_ref": None,
            "provider_usage_ref": "response-1",
            "actor_ref": "system:meter",
            "input_tokens": 2,
            "output_tokens": 1,
            "total_tokens": 3,
            "requests": 1,
        }
        args.update(overrides)
        return self.obs.record_usage("inv-1", **args)

    def rate(
        self,
        version_id="rate-input-v1",
        price_rate_ref="rate:provider-a:model-a:input",
        *,
        charge_type="input_tokens",
        currency="USD",
        price=1_000_000,
        provider="provider-a",
        model="model-a",
        prior=None,
        effective_from="2025-01-01T00:00:00+00:00",
    ):
        return self.obs.create_price_rate_version(
            price_rate_ref,
            version_id=version_id,
            provider=provider,
            model=model,
            charge_type=charge_type,
            unit_quantity=1_000_000,
            unit_price_micros=price,
            currency=currency,
            effective_from=effective_from,
            effective_until=None,
            source_ref="pricing:official",
            actor_ref="human:governance",
            prior_version_ref=prior,
        )

    def test_workflow_versions_and_tree_guards(self):
        first = self.workflow()
        self.assertEqual(first["version"], 1)
        child = self.obs.link_work_order(
            "workflow-1",
            "root",
            "child",
            link_id="link-1",
            actor_ref="agent:planner",
        )
        self.assertEqual(child["relation"], "decomposed_from")
        self.obs.link_work_order(
            "workflow-1",
            "child",
            "grandchild",
            link_id="link-2",
            actor_ref="agent:planner",
        )
        with self.assertRaises(ObservabilityConflict):
            self.obs.link_work_order(
                "workflow-1", "x", "x", link_id="self", actor_ref="agent:planner"
            )
        with self.assertRaises(ObservabilityConflict):
            self.obs.link_work_order(
                "workflow-1", "other", "child", link_id="multi", actor_ref="agent:planner"
            )
        with self.assertRaises(ObservabilityConflict):
            self.obs.link_work_order(
                "workflow-1", "orphan", "detached", link_id="orphan", actor_ref="agent:planner"
            )
        with self.assertRaises(ObservabilityConflict):
            self.obs.link_work_order(
                "workflow-1",
                "grandchild",
                "root",
                link_id="cycle",
                actor_ref="agent:planner",
            )
        second = self.obs.create_workflow_version(
            "workflow-1",
            version_id="workflow-v2",
            title="Research Wanhua",
            objective="Update and verify the thesis",
            scope_refs=["entity:wanhua"],
            root_work_order_refs=["root"],
            governance_policy_ref="policy-1",
            actor_ref="human:lumos",
            prior_version_ref="workflow-v1",
        )
        self.assertEqual(second["version"], 2)
        with self.assertRaises(ObservabilityConflict):
            self.obs.create_workflow_version(
                "workflow-1",
                version_id="branch",
                title="Branch",
                objective="Invalid branch",
                actor_ref="human:lumos",
                prior_version_ref="workflow-v1",
            )

    def test_shared_idempotency_is_explicit_and_cross_operation_safe(self):
        kwargs = {
            "version_id": "workflow-idem-v1",
            "title": "Idempotent workflow",
            "objective": "Test replay",
            "root_work_order_refs": ["root"],
            "actor_ref": "human:lumos",
            "idempotency_key": "observability-key",
        }
        first = self.obs.create_workflow_version("workflow-idem", **kwargs)
        duplicate = self.obs.create_workflow_version("workflow-idem", **kwargs)
        self.assertEqual(first["status"], "fresh")
        self.assertEqual(duplicate["status"], "duplicate")
        kwargs["objective"] = "Different request"
        self.assertEqual(
            self.obs.create_workflow_version("workflow-idem", **kwargs)["status"],
            "conflict",
        )
        self.assertEqual(
            self.obs.link_work_order(
                "workflow-idem",
                "root",
                "child",
                link_id="link-idem",
                actor_ref="agent:planner",
                idempotency_key="observability-key",
            )["status"],
            "conflict",
        )

    def test_usage_identity_nulls_correction_and_idempotency(self):
        self.workflow(roots=("work-1",))
        first = self.obs.record_usage(
            "inv-1",
            entry_id="unavailable-1",
            occurred_at=WHEN,
            metering_source="launcher_measured",
            measurement_status="unavailable",
            raw_usage={},
            workflow_ref="workflow-1",
            actor_ref="system:meter",
        )
        self.assertIsNone(first["input_tokens"])
        self.assertEqual(first["provider"], "provider-a")
        self.assertEqual(first["workflow_ref"], "workflow-1")
        with self.assertRaises(ObservabilityValidationError):
            self.obs.record_usage(
                "inv-1",
                entry_id="bad-unavailable",
                occurred_at=WHEN,
                metering_source="launcher_measured",
                measurement_status="unavailable",
                raw_usage={},
                actor_ref="system:meter",
                input_tokens=1,
            )
        corrected = self.obs.record_usage(
            "inv-1",
            entry_id="usage-correction",
            occurred_at=WHEN,
            metering_source="provider_reported",
            measurement_status="final",
            raw_usage={"total_tokens": 3},
            workflow_ref="workflow-1",
            actor_ref="system:meter",
            total_tokens=3,
            correction_of_ref="unavailable-1",
        )
        self.assertEqual(corrected["revision"], 2)
        self.assertEqual(self.obs.latest_usage("inv-1")["id"], "usage-correction")
        with self.assertRaises(ObservabilityConflict):
            self.obs.record_usage(
                "inv-1",
                entry_id="bad-branch",
                occurred_at=WHEN,
                metering_source="provider_reported",
                measurement_status="final",
                raw_usage={"total_tokens": 4},
                actor_ref="system:meter",
                total_tokens=4,
                correction_of_ref="unavailable-1",
            )

        self.store.register_invocation(invocation("inv-2"))
        args = {
            "entry_id": "usage-idem",
            "occurred_at": WHEN,
            "metering_source": "provider_reported",
            "measurement_status": "final",
            "raw_usage": {"total_tokens": 1},
            "actor_ref": "system:meter",
            "total_tokens": 1,
            "idempotency_key": "usage-key",
        }
        self.assertEqual(self.obs.record_usage("inv-2", **args)["status"], "fresh")
        self.assertEqual(self.obs.record_usage("inv-2", **args)["status"], "duplicate")
        args["total_tokens"] = 2
        self.assertEqual(self.obs.record_usage("inv-2", **args)["status"], "conflict")

    def test_price_and_cost_are_exact_versioned_and_currency_safe(self):
        self.usage()
        input_rate = self.rate()
        output_rate = self.rate(
            version_id="rate-output-v1",
            price_rate_ref="rate:provider-a:model-a:output",
            charge_type="output_tokens",
            price=2_000_000,
        )
        cost = self.obs.record_cost(
            "usage-1",
            cost_entry_id="cost-1",
            price_rate_refs=[input_rate["id"], output_rate["id"]],
            amount_micros=4,
            currency="USD",
            cost_status="estimated",
            calculation_ref="calculator:0.1",
            actor_ref="system:cost-ledger",
        )
        self.assertEqual(cost["amount_micros"], 4)
        self.assertEqual(cost["rounding_mode"], "half_up")
        actual = self.obs.record_cost(
            "usage-1",
            cost_entry_id="cost-2",
            price_rate_refs=[input_rate["id"], output_rate["id"]],
            amount_micros=4,
            currency="USD",
            cost_status="actual",
            calculation_ref="invoice:line-1",
            actor_ref="system:cost-ledger",
            correction_of_ref="cost-1",
        )
        self.assertEqual(actual["revision"], 2)
        self.assertEqual(self.obs.sum_costs(["cost-2"], currency="USD"), 4)

        eur = self.rate(
            version_id="rate-eur-v1",
            price_rate_ref="rate:provider-a:model-a:eur",
            charge_type="request",
            currency="EUR",
        )
        self.store.register_invocation(invocation("inv-eur"))
        self.obs.record_usage(
            "inv-eur",
            entry_id="usage-eur",
            occurred_at=WHEN,
            metering_source="provider_reported",
            measurement_status="final",
            raw_usage={"requests": 1},
            actor_ref="system:meter",
            requests=1,
        )
        eur_cost = self.obs.record_cost(
            "usage-eur",
            cost_entry_id="cost-eur",
            price_rate_refs=[eur["id"]],
            amount_micros=1,
            currency="EUR",
            cost_status="actual",
            calculation_ref="invoice:eur",
            actor_ref="system:cost-ledger",
        )
        with self.assertRaises(ObservabilityConflict):
            self.obs.sum_costs([cost["id"], eur_cost["id"]], currency="USD")
        with self.assertRaises(ObservabilityConflict):
            self.obs.record_cost(
                "usage-eur",
                cost_entry_id="mixed-currency",
                price_rate_refs=[eur["id"], input_rate["id"]],
                amount_micros=2,
                currency="EUR",
                cost_status="actual",
                calculation_ref="invalid",
                actor_ref="system:cost-ledger",
                correction_of_ref="cost-eur",
            )
        with self.assertRaises(ObservabilityConflict):
            self.obs.record_cost(
                "usage-eur",
                cost_entry_id="wrong-arithmetic",
                price_rate_refs=[eur["id"]],
                amount_micros=999,
                currency="EUR",
                cost_status="actual",
                calculation_ref="forged",
                actor_ref="system:cost-ledger",
                correction_of_ref="cost-eur",
            )

    def test_price_identity_and_effective_time_are_enforced(self):
        first = self.rate()
        second = self.rate(
            version_id="rate-input-v2",
            prior=first["id"],
            effective_from="2027-01-01T00:00:00+00:00",
            price=1_500_000,
        )
        self.assertEqual(second["version"], 2)
        with self.assertRaises(ObservabilityConflict):
            self.rate(
                version_id="rate-input-v3",
                prior=second["id"],
                effective_from="2028-01-01T00:00:00+00:00",
                model="different-model",
            )
        self.usage()
        with self.assertRaises(ObservabilityConflict):
            self.obs.record_cost(
                "usage-1",
                cost_entry_id="future-cost",
                price_rate_refs=[second["id"]],
                amount_micros=1,
                currency="USD",
                cost_status="estimated",
                calculation_ref="invalid-future-rate",
                actor_ref="system:cost-ledger",
            )

    def test_artifact_is_metadata_only_and_versioned(self):
        first = self.obs.register_artifact_version(
            "artifact:report",
            version_id="artifact-v1",
            title="Research report",
            kind="deliverable",
            media_type="application/pdf",
            artifact_content_hash="a" * 64,
            size_bytes=120,
            storage_locator="artifact-store:reports/one",
            producer_invocation_ref="inv-1",
            result_envelope_ref="result-1",
            result_envelope_hash="d" * 64,
            access_class="restricted",
            preview_status="redacted",
            actor_ref="system:artifact-registry",
        )
        self.assertEqual(first["work_order_ref"], "work-1")
        self.assertFalse(hasattr(self.obs, "read_artifact"))
        self.assertFalse(hasattr(self.obs, "open_artifact"))
        second = self.obs.register_artifact_version(
            "artifact:report",
            version_id="artifact-v2",
            title="Research report",
            kind="deliverable",
            media_type="application/pdf",
            artifact_content_hash="b" * 64,
            size_bytes=121,
            storage_locator="artifact-store:reports/two",
            producer_invocation_ref="inv-1",
            result_envelope_ref="result-2",
            result_envelope_hash="e" * 64,
            access_class="restricted",
            preview_status="unavailable",
            actor_ref="system:artifact-registry",
            prior_version_ref="artifact-v1",
        )
        self.assertEqual(second["version"], 2)
        with self.assertRaises(ObservabilityValidationError):
            self.obs.register_artifact_version(
                "artifact:inline",
                version_id="artifact-inline",
                title="Inline payload",
                kind="data",
                media_type="text/plain",
                artifact_content_hash="c" * 64,
                size_bytes=3,
                storage_locator="data:text/plain,abc",
                producer_invocation_ref="inv-1",
                result_envelope_ref="result-inline",
                result_envelope_hash="f" * 64,
                access_class="internal",
                preview_status="available",
                actor_ref="system:artifact-registry",
            )

    def test_authority_rows_are_immutable_and_direct_insert_is_guarded(self):
        self.workflow()
        self.obs.link_work_order(
            "workflow-1", "root", "child", link_id="link-1", actor_ref="agent:planner"
        )
        self.usage()
        self.rate()
        self.obs.record_cost(
            "usage-1",
            cost_entry_id="cost-1",
            price_rate_refs=["rate-input-v1"],
            amount_micros=2,
            currency="USD",
            cost_status="estimated",
            calculation_ref="calculator:0.1",
            actor_ref="system:cost-ledger",
        )
        self.obs.register_artifact_version(
            "artifact:report",
            version_id="artifact-v1",
            title="Report",
            kind="deliverable",
            media_type="text/plain",
            artifact_content_hash="a" * 64,
            size_bytes=1,
            storage_locator="artifact-store:one",
            producer_invocation_ref="inv-1",
            result_envelope_ref="result-1",
            result_envelope_hash="d" * 64,
            access_class="internal",
            preview_status="unavailable",
            actor_ref="system:artifact-registry",
        )
        tables = [
            "observability_workflow_versions",
            "observability_work_order_links",
            "observability_usage_entries",
            "observability_price_rate_versions",
            "observability_cost_entries",
            "observability_artifact_versions",
        ]
        for table in tables:
            with self.subTest(table=table):
                with self.assertRaises(sqlite3.DatabaseError):
                    self.obs.conn.execute(f"UPDATE {table} SET content_hash='evil'")
                with self.assertRaises(sqlite3.DatabaseError):
                    self.obs.conn.execute(f"DELETE FROM {table}")
        with self.assertRaises(sqlite3.DatabaseError):
            self.obs.conn.execute(
                "INSERT INTO observability_workflow_versions"
                "(version_id,workflow_ref,version_number,root_work_order_refs_json,record_json,content_hash,created_at) "
                "VALUES('raw','raw',1,'[]','{}','bad','now')"
            )

    def test_stored_wires_match_closed_schemas_and_hashes(self):
        workflow = self.workflow()
        link = self.obs.link_work_order(
            "workflow-1", "root", "child", link_id="link-1", actor_ref="agent:planner"
        )
        usage = self.usage()
        rate = self.rate()
        cost = self.obs.record_cost(
            "usage-1",
            cost_entry_id="cost-1",
            price_rate_refs=[rate["id"]],
            amount_micros=2,
            currency="USD",
            cost_status="estimated",
            calculation_ref="calculator:0.1",
            actor_ref="system:cost-ledger",
        )
        artifact = self.obs.register_artifact_version(
            "artifact:report",
            version_id="artifact-v1",
            title="Report",
            kind="deliverable",
            media_type="text/plain",
            artifact_content_hash="a" * 64,
            size_bytes=1,
            storage_locator="artifact-store:one",
            producer_invocation_ref="inv-1",
            result_envelope_ref="result-1",
            result_envelope_hash="d" * 64,
            access_class="internal",
            preview_status="unavailable",
            actor_ref="system:artifact-registry",
        )
        cases = {
            "workflow-run-version.schema.json": workflow,
            "work-order-link.schema.json": link,
            "usage-entry.schema.json": usage,
            "price-rate-version.schema.json": rate,
            "cost-entry.schema.json": cost,
            "artifact-version.schema.json": artifact,
        }
        for filename, result in cases.items():
            with self.subTest(schema=filename):
                schema = json.loads((CONTRACTS / filename).read_text(encoding="utf-8"))
                wire = {key: value for key, value in result.items() if key not in {"status", "idempotency_key"}}
                self.assertEqual(set(wire), set(schema["required"]))
                self.assertEqual(set(schema["properties"]), set(schema["required"]))
                expected = content_hash({key: value for key, value in wire.items() if key != "content_hash"})
                self.assertEqual(wire["content_hash"], expected)

    def test_missing_references_and_unpriced_semantics(self):
        with self.assertRaises(ObservabilityNotFound):
            self.obs.record_usage(
                "missing",
                entry_id="missing-usage",
                occurred_at=WHEN,
                metering_source="provider_reported",
                measurement_status="final",
                raw_usage={"total_tokens": 1},
                total_tokens=1,
                actor_ref="system:meter",
            )
        with self.assertRaises(ObservabilityNotFound):
            self.obs.record_usage(
                "inv-1",
                entry_id="bad-workflow",
                occurred_at=WHEN,
                metering_source="provider_reported",
                measurement_status="final",
                raw_usage={"total_tokens": 1},
                total_tokens=1,
                workflow_ref="workflow:missing",
                actor_ref="system:meter",
            )
        self.usage()
        unpriced = self.obs.record_cost(
            "usage-1",
            cost_entry_id="unpriced",
            price_rate_refs=[],
            amount_micros=None,
            currency="USD",
            cost_status="unpriced",
            calculation_ref="pricing:missing",
            actor_ref="system:cost-ledger",
        )
        self.assertIsNone(unpriced["amount_micros"])
        with self.assertRaises(ObservabilityConflict):
            self.obs.sum_costs(["unpriced"], currency="USD")


if __name__ == "__main__":
    unittest.main()
