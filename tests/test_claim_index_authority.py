import copy
import json
import unittest
from unittest.mock import patch

from dalton_core.research_context import (
    ResearchContextError,
    build_claim_index,
)
from dalton_core.store import DaltonStore, content_hash
from tests.test_connector import CONTRACTS, validate_json_schema


def invocation(invocation_id: str, family: str) -> dict:
    return {
        "schema_version": "0.1",
        "id": invocation_id,
        "created_at": "2026-08-15T00:00:00+00:00",
        "work_order_ref": "wo-claim-index",
        "profile_ref": "profile-" + invocation_id,
        "granularity": "task",
        "capability": "research",
        "provider": family,
        "model": "model-" + invocation_id,
        "model_family": family,
        "runtime_ref": "runtime-claim-index",
        "actor_ref": "actor-claim-index",
        "usage": {"tokens": 1},
        "input_refs": [],
        "output_refs": [],
        "started_at": "2026-08-15T00:00:00+00:00",
        "completed_at": None,
        "side_effects": [],
        "parent_ref": None,
    }


class ClaimIndexAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = DaltonStore(":memory:")
        self.addCleanup(self.store.close)

    def add_claim(self, ref: str, *, value=None, kind="qualitative", unit=None):
        producer = "producer-" + ref
        self.store.register_invocation(invocation(producer, "family-a"))
        evidence = self.store.register_evidence({
            "evidence_ref": "evidence-" + ref,
            "source_type": "filing",
            "source_ref": "sec:" + ref,
            "retrieved_at": "2026-08-15T00:00:00+00:00",
            "source_lineage": ["sec:" + ref],
            "independence_group": "sec:" + ref,
            "actor_ref": "actor-claim-index",
        })
        claim = self.store.register_claim({
            "claim_ref": ref,
            "subject_ref": "company:fixture",
            "metric_or_aspect": "revenue" if kind == "quantitative" else "business",
            "period": "2026Q2",
            "basis": "reported",
            "normalized_statement": "claim " + ref,
            "claim_kind": kind,
            "value": value,
            "unit": unit,
            "producer_invocation_refs": [producer],
            "actor_ref": "actor-claim-index",
        })
        self.store.relate_evidence({
            "id": "relation-" + ref,
            "evidence_version_ref": evidence["evidence_version_id"],
            "claim_version_ref": claim["claim_version_id"],
            "relation": "supports",
        })
        return claim

    def test_status_is_projected_and_snapshot_hash_binds_adjudication(self) -> None:
        claim = self.add_claim("status")
        snapshot = self.store.claim_index_snapshot(
            created_at="2026-08-15T01:00:00+00:00"
        )
        schema = json.loads(
            (CONTRACTS / "claim-index-ledger-snapshot.schema.json").read_text(
                encoding="utf-8"
            )
        )
        validate_json_schema(snapshot, schema, schema)
        before = build_claim_index(
            ledger=self.store,
            created_at="2026-08-15T01:00:00+00:00",
        )
        self.assertEqual(before["entries"][0]["status"], "proposed")
        before_updated_at = before["entries"][0]["updated_at"]
        self.store.register_invocation(invocation("adjudicator-status", "family-b"))
        self.store.adjudicate_claim({
            "claim_version_ref": claim["claim_version_id"],
            "adjudicated_status": "corroborated",
            "rationale": "independent review",
            "findings": [],
            "adjudicator_invocation_ref": "adjudicator-status",
            "subject_invocation_refs": ["producer-status"],
        })
        after = build_claim_index(
            ledger=self.store,
            created_at="2026-08-15T01:00:00+00:00",
        )
        validate_json_schema(
            self.store.claim_index_snapshot(
                created_at="2026-08-15T01:00:00+00:00"
            ),
            schema,
            schema,
        )
        self.assertEqual(after["entries"][0]["status"], "corroborated")
        self.assertGreater(after["entries"][0]["updated_at"], before_updated_at)
        self.assertNotEqual(
            before["ledger_snapshot_ref"], after["ledger_snapshot_ref"]
        )
        self.assertNotEqual(
            before["ledger_snapshot_hash"], after["ledger_snapshot_hash"]
        )
        self.assertNotEqual(before["content_hash"], after["content_hash"])
        with self.assertRaises(ResearchContextError):
            build_claim_index(
                ledger=self.store,
                ledger_snapshot_ref=after["ledger_snapshot_ref"],
                ledger_snapshot_hash=after["ledger_snapshot_hash"],
                created_at="2026-08-15T01:00:00+00:00",
            )

        tampered = copy.deepcopy(
            self.store.claim_index_snapshot(
                created_at="2026-08-15T01:00:00+00:00"
            )
        )
        tampered["latest_adjudications"][0]["adjudication"][
            "adjudicated_status"
        ] = "retracted"
        tampered["content_hash"] = content_hash({
            key: value for key, value in tampered.items() if key != "content_hash"
        })
        with patch.object(self.store, "claim_index_snapshot", return_value=tampered):
            with self.assertRaises(ResearchContextError):
                build_claim_index(
                    ledger=self.store,
                    created_at="2026-08-15T01:00:00+00:00",
                )

    def test_contested_projection_cannot_be_forged_by_caller_bundle(self) -> None:
        self.add_claim("numeric-a", value=100, kind="quantitative", unit="USDm")
        self.add_claim("numeric-b", value=120, kind="quantitative", unit="USDm")
        index = build_claim_index(
            ledger=self.store,
            created_at="2026-08-15T01:00:00+00:00",
        )
        self.assertEqual(
            {entry["status"] for entry in index["entries"]}, {"contested"}
        )
        snapshot = self.store.claim_index_snapshot(
            created_at="2026-08-15T01:00:00+00:00"
        )
        schema = json.loads(
            (CONTRACTS / "claim-index-ledger-snapshot.schema.json").read_text(
                encoding="utf-8"
            )
        )
        validate_json_schema(snapshot, schema, schema)
        tampered = copy.deepcopy(snapshot)
        tampered["claim_challenges"][0]["semantic_key"][0] = "company:forged"
        tampered["id"] = "ledger-snapshot:claim-index:" + content_hash({
            key: value
            for key, value in tampered.items()
            if key not in {"id", "content_hash"}
        })
        tampered["content_hash"] = content_hash({
            key: value for key, value in tampered.items() if key != "content_hash"
        })
        with patch.object(self.store, "claim_index_snapshot", return_value=tampered):
            with self.assertRaises(ResearchContextError):
                build_claim_index(
                    ledger=self.store,
                    created_at="2026-08-15T01:00:00+00:00",
                )
        with self.assertRaises(ResearchContextError):
            build_claim_index(
                [{"claim": {}, "evidence_relations": [], "status": "corroborated"}],
                ledger_snapshot_ref="ledger-snapshot:forged",
                ledger_snapshot_hash="0" * 64,
                created_at="2026-08-15T01:00:00+00:00",
            )

    def test_numeric_comparison_preserves_v01_key_and_v02_dimensions(self) -> None:
        base = {
            "subject_ref": "company:fixture",
            "metric_or_aspect": "revenue",
            "period": "2026Q2",
            "basis": "reported",
            "unit": "millions",
        }
        legacy_key = DaltonStore._claim_semantic_key(
            {**base, "schema_version": "0.1"}
        )
        usd_key = DaltonStore._claim_semantic_key(
            {
                **base,
                "schema_version": "0.2",
                "currency": "USD",
                "scale": "million",
            }
        )
        eur_key = DaltonStore._claim_semantic_key(
            {
                **base,
                "schema_version": "0.2",
                "currency": "EUR",
                "scale": "million",
            }
        )
        self.assertEqual(len(legacy_key), 5)
        self.assertNotEqual(usd_key, eur_key)
        self.assertTrue(DaltonStore._claim_values_equal(100, "100.0"))

    def test_snapshot_normalizes_wrapper_time_without_rewriting_claim(self) -> None:
        self.store.register_invocation(invocation("producer-time", "family-a"))
        claim = self.store.register_claim({
            "claim_ref": "time",
            "created_at": "2026-08-15T00:00:00+00:00",
            "subject_ref": "company:fixture",
            "metric_or_aspect": "business",
            "period": "2026Q2",
            "basis": "reported",
            "normalized_statement": "timestamp normalization remains a projection",
            "claim_kind": "qualitative",
            "producer_invocation_refs": ["producer-time"],
            "actor_ref": "actor-claim-index",
        })
        index = build_claim_index(
            ledger=self.store,
            created_at="2026-08-15T01:00:00+00:00",
        )
        self.assertEqual(
            index["entries"][0]["claim_version_ref"], claim["claim_version_id"]
        )
        self.assertEqual(
            index["entries"][0]["updated_at"],
            "2026-08-15T00:00:00.000000+00:00",
        )

    def test_old_claim_version_is_superseded_and_does_not_inherit_v2_adjudication(self) -> None:
        first = self.add_claim("versioned")
        self.store.register_invocation(invocation("adjudicator-v1", "family-b"))
        self.store.adjudicate_claim({
            "claim_version_ref": first["claim_version_id"],
            "adjudicated_status": "corroborated",
            "rationale": "v1 review",
            "findings": [],
            "adjudicator_invocation_ref": "adjudicator-v1",
            "subject_invocation_refs": ["producer-versioned"],
        })
        self.store.register_invocation(invocation("producer-versioned-v2", "family-a"))
        second = self.store.register_claim({
            "claim_ref": "versioned",
            "subject_ref": "company:fixture",
            "metric_or_aspect": "business",
            "period": "2026Q2",
            "basis": "reported",
            "normalized_statement": "claim versioned v2",
            "claim_kind": "qualitative",
            "producer_invocation_refs": ["producer-versioned-v2"],
            "actor_ref": "actor-claim-index",
            "prior_version_ref": first["claim_version_id"],
        })
        self.assertEqual(
            self.store.get_claim(first["claim_version_id"])["status"], "superseded"
        )
        self.assertEqual(
            self.store.get_claim(second["claim_version_id"])["status"], "proposed"
        )
        self.store.register_invocation(invocation("adjudicator-v2", "family-c"))
        self.store.adjudicate_claim({
            "claim_version_ref": second["claim_version_id"],
            "adjudicated_status": "retracted",
            "rationale": "v2 review",
            "findings": [],
            "adjudicator_invocation_ref": "adjudicator-v2",
            "subject_invocation_refs": ["producer-versioned-v2"],
        })
        self.assertEqual(
            self.store.get_claim(first["claim_version_id"])["status"], "superseded"
        )
        self.assertEqual(
            self.store.get_claim(second["claim_version_id"])["status"], "retracted"
        )
        index = build_claim_index(
            ledger=self.store,
            claim_version_refs=[first["claim_version_id"], second["claim_version_id"]],
            created_at="2026-08-15T02:00:00+00:00",
        )
        statuses = {
            entry["claim_version_ref"]: entry["status"] for entry in index["entries"]
        }
        self.assertEqual(statuses[first["claim_version_id"]], "superseded")
        self.assertEqual(statuses[second["claim_version_id"]], "retracted")


if __name__ == "__main__":
    unittest.main()
