import json
import sqlite3
import unittest

from dalton_core.capability_registry import (
    CapabilityConflict,
    CapabilityRegistry,
    EvaluationRejected,
    PermissionEscalation,
    PromotionRejected,
)
from dalton_core.store import DaltonStore


FIXTURE_MANIFEST_HASH = "f" * 64
ENV_HASH_1 = "1" * 64
ENV_HASH_2 = "2" * 64
ENV_HASH = "e" * 64


def invocation(identifier, family):
    return {
        "schema_version": "0.1", "id": identifier,
        "created_at": "2026-01-01T00:00:00+00:00",
        "work_order_ref": "capability-test", "profile_ref": f"profile-{identifier}",
        "granularity": "task", "capability": "capability-builder",
        "provider": family, "model": f"model-{identifier}", "model_family": family,
        "runtime_ref": "trusted-test", "actor_ref": f"agent:{identifier}",
        "usage": {"tokens": 1}, "input_refs": [], "output_refs": [],
        "started_at": "2026-01-01T00:00:00+00:00", "completed_at": None,
        "side_effects": [], "parent_ref": None,
    }


def proposal(identifier="cap-v1", prior=None, permissions=None):
    return {
        "schema_version": "0.1", "id": identifier,
        "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00",
        "title": "Normalize vendor feed", "kind": "tool",
        "gap": {"description": "repeated formatting"}, "expected_benefit": {"minutes": 10},
        "contract": {"input": "json", "output": "json"},
        "permissions": permissions or {"filesystem": {"read": True}, "network": False},
        "fixtures": ["fixture:1"],
        "fixture_manifest_hash": FIXTURE_MANIFEST_HASH,
        "participants": {"builder_invocation_ref": "builder-1", "actor_ref": "agent:builder"},
        "status": "proposed", "artifact_refs": [f"artifact:{identifier}"],
        "prior_capability_ref": prior,
    }


class CapabilityRegistryTests(unittest.TestCase):
    def setUp(self):
        self.store = DaltonStore(":memory:")
        self.registry = CapabilityRegistry(self.store)
        self.store.register_invocation(invocation("builder-1", "family-a"))
        self.store.register_invocation(invocation("evaluator-1", "family-b"))
        self.store.register_invocation(invocation("evaluator-same", "family-a"))
        self.addCleanup(self.store.close)
        self.registry.submit_proposal(proposal())

    def evaluate(self, evaluation_id="eval-1", proposal_ref="cap-v1", results=None):
        return self.registry.record_evaluation(
            proposal_ref, evaluation_id=evaluation_id, fixtures=["fixture:1"],
            baseline={"output": "old"}, results={"status": "pass"} if results is None else results,
            environment_hash=ENV_HASH_1, evaluator_invocation="evaluator-1",
        )

    def test_proposal_eval_human_approve_active_and_revision_chain(self):
        self.assertEqual(self.evaluate()["status"], "fresh")
        approved = self.registry.decide_promotion(
            "cap-v1", decision="approve", actor_ref="human:lumos",
            evaluation_id="eval-1", requested_permissions={"filesystem": {"read": True}, "network": False},
            rationale="fixture evaluation passed",
        )
        self.assertEqual(approved["status"], "fresh")
        pointer = self.registry.active_pointer("cap-v1")
        self.assertEqual(pointer["revision_id"], "cap-v1")
        revision = proposal("cap-v2", prior="cap-v1")
        revision["participants"]["builder_invocation_ref"] = "builder-1"
        self.registry.submit_proposal(revision)
        self.registry.record_evaluation(
            "cap-v2", evaluation_id="eval-2", fixtures=["fixture:1"], baseline={},
            results={"passed": True}, environment_hash=ENV_HASH_2, evaluator_invocation="evaluator-1",
        )
        self.registry.decide_promotion("cap-v2", decision="approve", actor_ref="human:lumos", evaluation_id="eval-2")
        self.assertEqual(self.registry.active_pointer("cap-v1")["revision_id"], "cap-v2")
        self.assertEqual(len(self.registry.pointer_history("cap-v1")), 2)

    def test_agent_self_approve_and_builder_evaluator_rejected(self):
        with self.assertRaises(PromotionRejected):
            self.registry.decide_promotion("cap-v1", decision="approve", actor_ref="agent:builder", evaluation_id="eval-1")
        with self.assertRaises(EvaluationRejected):
            self.registry.record_evaluation(
                "cap-v1", evaluation_id="self-eval", fixtures=["fixture:1"], baseline={}, results={"status": "pass"},
                environment_hash=ENV_HASH, evaluator_invocation="builder-1",
            )
        with self.assertRaises(EvaluationRejected):
            self.registry.record_evaluation(
                "cap-v1", evaluation_id="same-family", fixtures=["fixture:1"], baseline={}, results={"status": "pass"},
                environment_hash=ENV_HASH, evaluator_invocation="evaluator-same",
            )

    def test_permission_escalation_is_rejected(self):
        self.evaluate()
        with self.assertRaises(PermissionEscalation):
            self.registry.decide_promotion(
                "cap-v1", decision="approve", actor_ref="human:lumos", evaluation_id="eval-1",
                requested_permissions={"filesystem": {"read": True, "write": True}, "network": True},
            )

    def test_rollback_is_append_only(self):
        self.evaluate()
        self.registry.decide_promotion("cap-v1", decision="approve", actor_ref="human:lumos", evaluation_id="eval-1")
        revision = proposal("cap-v2", prior="cap-v1")
        self.registry.submit_proposal(revision)
        self.registry.record_evaluation("cap-v2", evaluation_id="eval-2", fixtures=["fixture:1"], baseline={}, results={"status": "pass"}, environment_hash=ENV_HASH, evaluator_invocation="evaluator-1")
        self.registry.decide_promotion("cap-v2", decision="approve", actor_ref="human:lumos", evaluation_id="eval-2")
        rollback = self.registry.rollback("cap-v1", "cap-v1", actor_ref="human:lumos")
        self.assertEqual(rollback["decision"], "rollback")
        self.assertEqual(self.registry.active_pointer("cap-v1")["revision_id"], "cap-v1")
        self.assertEqual([r["action"] for r in self.registry.pointer_history("cap-v1")], ["active", "active", "rollback"])

    def test_collision_idempotency_and_direct_write_immutability(self):
        first = self.registry.submit_proposal(proposal("cap-idem"), idempotency_key="proposal-key")
        duplicate = self.registry.submit_proposal(proposal("cap-idem"), idempotency_key="proposal-key")
        self.assertEqual(duplicate["status"], "duplicate")
        different = proposal("cap-other")
        self.assertEqual(self.registry.submit_proposal(different, idempotency_key="proposal-key")["status"], "conflict")
        with self.assertRaises(sqlite3.DatabaseError):
            self.registry.conn.execute("UPDATE capability_proposal_versions SET content_hash='evil'")
        with self.assertRaises(sqlite3.DatabaseError):
            self.registry.conn.execute("DELETE FROM capability_proposal_versions")
        self.evaluate()
        self.registry.decide_promotion("cap-v1", decision="approve", actor_ref="human:lumos", evaluation_id="eval-1", idempotency_key="decision-key")
        conflict = self.registry.decide_promotion("cap-v1", decision="approve", actor_ref="human:lumos", evaluation_id="eval-1", idempotency_key="decision-key", rationale="different")
        self.assertEqual(conflict["status"], "conflict")

    def test_external_sandbox_evidence_only(self):
        self.assertFalse(hasattr(self.registry, "execute"))
        result = self.evaluate(results={"passed": True, "sandbox": "external", "tool_output_hash": "abc"})
        saved = self.registry.conn.execute("SELECT results_json FROM capability_evaluations WHERE evaluation_id=?", (result["evaluation_id"],)).fetchone()[0]
        self.assertEqual(json.loads(saved)["sandbox"], "external")

    def test_inline_invocations_and_empty_evidence_are_rejected(self):
        with self.assertRaises(Exception):
            self.registry.submit_proposal(proposal("inline-proposal"), builder_invocation=invocation("inline-builder", "family-a"))
        with self.assertRaises(Exception):
            self.registry.record_evaluation(
                "cap-v1", evaluation_id="inline-eval", fixtures=["fixture:1"], baseline={},
                results={"status": "pass"}, environment_hash=ENV_HASH, evaluator_invocation=invocation("inline-evaluator", "family-b"),
            )
        self.evaluate(results={})
        with self.assertRaises(PromotionRejected):
            self.registry.decide_promotion("cap-v1", decision="approve", actor_ref="human:lumos", evaluation_id="eval-1")
        self.evaluate(evaluation_id="eval-pass", results={"status": "passed"})
        self.assertEqual(self.registry.decide_promotion("cap-v1", decision="approve", actor_ref="human:lumos", evaluation_id="eval-pass")["status"], "fresh")

    def test_default_permissions_and_revision_provenance_collision(self):
        self.evaluate()
        self.registry.decide_promotion("cap-v1", decision="approve", actor_ref="human:lumos", evaluation_id="eval-1")
        saved = json.loads(self.registry.conn.execute("SELECT requested_permissions_json FROM capability_decisions ORDER BY created_at DESC LIMIT 1").fetchone()[0])
        self.assertEqual(saved, proposal()["permissions"])
        changed = proposal("cap-v1")
        changed["participants"]["actor_ref"] = "agent:other"
        with self.assertRaises(CapabilityConflict):
            self.registry.submit_proposal(changed)

    def test_policy_change_after_evaluation_requires_reevaluation(self):
        self.evaluate()
        self.store.create_policy({"allowed_verdicts": ["pass"]}, version_number=2)
        with self.assertRaises(PromotionRejected):
            self.registry.decide_promotion("cap-v1", decision="approve", actor_ref="human:lumos", evaluation_id="eval-1")

    def test_unapproved_target_cannot_be_rollback_target(self):
        self.evaluate()
        self.registry.decide_promotion("cap-v1", decision="approve", actor_ref="human:lumos", evaluation_id="eval-1")
        revision = proposal("cap-v2", prior="cap-v1")
        self.registry.submit_proposal(revision)
        self.registry.record_evaluation("cap-v2", evaluation_id="eval-2", fixtures=["fixture:1"], baseline={}, results={"status": "pass"}, environment_hash=ENV_HASH, evaluator_invocation="evaluator-1")
        with self.assertRaises(PromotionRejected):
            self.registry.rollback("cap-v1", "cap-v2", actor_ref="human:lumos")

        fresh_store = DaltonStore(":memory:")
        fresh_registry = CapabilityRegistry(fresh_store)
        self.addCleanup(fresh_store.close)
        fresh_store.register_invocation(invocation("builder-1", "family-a"))
        fresh_registry.submit_proposal(proposal())
        with self.assertRaises(PromotionRejected):
            fresh_registry.rollback("cap-v1", "cap-v1", actor_ref="human:lumos")

    def test_non_proposed_payload_cannot_enter_registry(self):
        active = proposal("active-payload")
        active["status"] = "active"
        with self.assertRaises(Exception):
            self.registry.submit_proposal(active)

    def test_fixture_declaration_is_nonempty_unique_and_exactly_replayed(self):
        empty = proposal("empty-fixtures")
        empty["fixtures"] = []
        with self.assertRaises(Exception):
            self.registry.submit_proposal(empty)
        duplicate = proposal("duplicate-fixtures")
        duplicate["fixtures"] = ["fixture:1", "fixture:1"]
        with self.assertRaises(Exception):
            self.registry.submit_proposal(duplicate)
        with self.assertRaises(EvaluationRejected):
            self.registry.record_evaluation(
                "cap-v1", evaluation_id="wrong-fixtures", fixtures=["other-fixture"],
                baseline={}, results={"status": "pass"}, environment_hash=ENV_HASH, evaluator_invocation="evaluator-1",
            )
        with self.assertRaises(EvaluationRejected):
            self.registry.record_evaluation(
                "cap-v1", evaluation_id="duplicate-eval-fixtures", fixtures=["fixture:1", "fixture:1"],
                baseline={}, results={"status": "pass"}, environment_hash=ENV_HASH, evaluator_invocation="evaluator-1",
            )

    def test_registry_hash_fields_use_one_sha256_wire_format(self):
        with self.assertRaises(Exception):
            self.registry.submit_proposal(
                proposal("bad-artifact-hash"), artifact_hash="artifact-not-sha256"
            )
        bad_manifest = proposal("bad-fixture-manifest")
        bad_manifest["fixture_manifest_hash"] = "fixture-not-sha256"
        with self.assertRaises(Exception):
            self.registry.submit_proposal(bad_manifest)
        with self.assertRaises(EvaluationRejected):
            self.registry.record_evaluation(
                "cap-v1",
                evaluation_id="bad-environment-hash",
                fixtures=["fixture:1"],
                baseline={},
                results={"status": "pass"},
                environment_hash="env-not-sha256",
                evaluator_invocation="evaluator-1",
            )

    def test_policy_effective_window_is_enforced_for_evaluation_and_approval(self):
        self.store.create_policy({"allowed_verdicts": ["pass"], "effective_from": "2999-01-01T00:00:00+00:00"}, version_number=2)
        with self.assertRaises(PromotionRejected):
            self.evaluate(evaluation_id="future-policy-eval")

    def test_human_actor_format_and_permission_null_are_rejected(self):
        self.evaluate()
        with self.assertRaises(PromotionRejected):
            self.registry.decide_promotion("cap-v1", decision="approve", actor_ref="human:", evaluation_id="eval-1")
        with self.assertRaises(PermissionEscalation):
            self.registry.decide_promotion("cap-v1", decision="approve", actor_ref="human:lumos", evaluation_id="eval-1", requested_permissions={"network": None})
        null_permissions = proposal("null-permissions")
        null_permissions["permissions"] = {"network": None}
        with self.assertRaises(PermissionEscalation):
            self.registry.submit_proposal(null_permissions)

    def test_evaluation_id_is_required_for_idempotent_evidence(self):
        with self.assertRaises(EvaluationRejected):
            self.registry.record_evaluation(
                "cap-v1", fixtures=["fixture:1"], baseline={}, results={"status": "pass"},
                environment_hash=ENV_HASH, evaluator_invocation="evaluator-1", idempotency_key="eval-key",
            )


if __name__ == "__main__":
    unittest.main()
