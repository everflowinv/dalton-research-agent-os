import sqlite3
import unittest

from dalton_core.errors import GateRejected, IdempotencyConflict, IndependenceViolation, NotFound, ValidationError
from dalton_core.store import DaltonStore


def inv(i, family):
    return {
        "schema_version": "0.1", "id": i, "created_at": "2026-01-01T00:00:00+00:00",
        "work_order_ref": "wo-ledger", "profile_ref": "profile-" + i, "granularity": "task",
        "capability": "research", "provider": family, "model": "model-" + i,
        "model_family": family, "runtime_ref": "runtime", "actor_ref": "actor",
        "usage": {"tokens": 1}, "input_refs": [], "output_refs": [],
        "started_at": "2026-01-01T00:00:00+00:00", "completed_at": None,
        "side_effects": [], "parent_ref": None,
    }


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.s = DaltonStore(":memory:")
        self.addCleanup(self.s.close)

    def evidence(self, ref="ev-1"):
        return self.s.register_evidence({
            "evidence_ref": ref, "source_type": "filing", "source_ref": "sec:" + ref,
            "retrieved_at": "2026-01-01T00:00:00+00:00", "source_lineage": ["sec:" + ref],
            "independence_group": "sec:" + ref, "actor_ref": "researcher",
        })

    def claim(self, ref="claim-1", value=None, kind="qualitative", unit=None):
        producer_id = "producer-" + ref
        # Claims may only reference invocations already owned by Core.
        self.s.register_invocation(inv(producer_id, "family-a"))
        return self.s.register_claim({
            "claim_ref": ref, "subject_ref": "company:x", "metric_or_aspect": "margin",
            "period": "2026Q2", "basis": "reported", "normalized_statement": "margin claim " + ref,
            "claim_kind": kind, "value": value, "unit": unit, "producer_invocation_refs": [producer_id], "actor_ref": "researcher",
        })

    def test_evidence_claim_relation_and_immutable_versions(self):
        evidence = self.evidence()
        claim = self.claim()
        relation = self.s.relate_evidence({
            "id": "relation-ev-claim",
            "evidence_version_ref": evidence["evidence_version_id"],
            "claim_version_ref": claim["claim_version_id"], "relation": "supports",
        })
        self.assertEqual(relation["claim_ref"], "claim-1")
        self.assertEqual(self.s.get_claim(claim["claim_version_id"])["status"], "proposed")
        with self.assertRaises(sqlite3.DatabaseError):
            self.s.conn.execute("UPDATE claim_versions SET claim_json='{}'")
        with self.assertRaises(sqlite3.DatabaseError):
            self.s.conn.execute("DELETE FROM evidence_relations")

    def test_numeric_conflict_is_deterministic_challenge_and_projection(self):
        first = self.claim("claim-a", 100, "quantitative", "USDm")
        second = self.claim("claim-b", 120, "quantitative", "USDm")
        self.assertEqual(self.s.get_claim(first["claim_version_id"])["status"], "contested")
        self.assertEqual(self.s.get_claim(second["claim_version_id"])["status"], "contested")
        challenges = self.s.list_claim_challenges()
        self.assertEqual(len(challenges), 1)
        self.assertEqual(challenges[0]["challenge"]["reason"], "exact numeric claims conflict")

    def test_qualitative_adjudication_requires_independent_invocation(self):
        claim = self.claim()
        self.s.register_invocation(inv("adjudicator", "family-b"))
        result = self.s.adjudicate_claim({
            "claim_version_ref": claim["claim_version_id"], "adjudicated_status": "corroborated",
            "rationale": "independent review", "findings": [],
            "adjudicator_invocation_ref": "adjudicator", "subject_invocation_refs": ["producer-claim-1"],
        })
        self.assertEqual(result["status"], "corroborated")
        self.assertEqual(self.s.get_claim(claim["claim_version_id"])["status"], "corroborated")
        with self.assertRaises(IndependenceViolation):
            self.s.adjudicate_claim({
                "claim_version_ref": claim["claim_version_id"], "adjudicated_status": "retracted",
                "rationale": "self review", "adjudicator_invocation_ref": "producer-claim-1",
            })

    def test_claim_status_cannot_be_written_and_thesis_refs_are_checked(self):
        with self.assertRaises((ValidationError, NotFound)):
            self.claim_bad = self.s.register_claim({
                "claim_ref": "bad", "subject_ref": "x", "metric_or_aspect": "m", "period": "p",
                "basis": "b", "normalized_statement": "s", "claim_kind": "qualitative", "status": "corroborated",
            })
        self.s.stage_change("c-missing", thesis_id="t", content={
                "statement": "s", "mechanism": "m", "confidence": "medium", "implied_expectation": "e",
                "claim_refs": ["not-a-version"], "catalyst_refs": [], "falsifier_refs": [], "change_reason": "test",
            }, producer_invocation=inv("producer-missing", "family-a"))
        self.s.verify_change("c-missing", verification_id="v-missing", verifier_invocation=inv("verifier-missing", "family-b"), verdict="pass", findings=[])
        with self.assertRaises(GateRejected):
            self.s.commit("c-missing", "v-missing", "missing-claim")
        claim = self.claim()
        evidence = self.evidence("commit-support")
        self.s.relate_evidence({
            "id": "relation-commit",
            "evidence_version_ref": evidence["evidence_version_id"],
            "claim_version_ref": claim["claim_version_id"], "relation": "supports",
        })
        self.s.stage_change("c", thesis_id="t", content={
            "statement": "s", "mechanism": "m", "confidence": "medium", "implied_expectation": "e",
            "claim_refs": [claim["claim_version_id"]], "catalyst_refs": [], "falsifier_refs": [], "change_reason": "test",
        }, producer_invocation=inv("producer-2", "family-a"))
        self.s.verify_change("c", verification_id="v", verifier_invocation=inv("verifier", "family-b"), verdict="pass", findings=[])
        self.assertEqual(self.s.commit("c", "v", "ledger-commit")["status"], "fresh")

    def test_thesis_claim_requires_evidence_relation(self):
        claim = self.claim("ungrounded")
        self.s.stage_change("unrelated", thesis_id="t", content={
            "statement": "s", "mechanism": "m", "confidence": "medium", "implied_expectation": "e",
            "claim_refs": [claim["claim_version_id"]], "catalyst_refs": [], "falsifier_refs": [], "change_reason": "test",
        }, producer_invocation=inv("unrelated-producer", "family-a"))
        self.s.verify_change("unrelated", verification_id="unrelated-verifier", verifier_invocation=inv("unrelated-verifier", "family-b"), verdict="pass", findings=[])
        with self.assertRaises(GateRejected):
            self.s.commit("unrelated", "unrelated-verifier", "unrelated-commit")

        for relation_type in ("supports", "contradicts", "qualifies"):
            c = self.claim("relation-" + relation_type)
            e = self.evidence("evidence-" + relation_type)
            self.s.relate_evidence({
                "id": "relation-" + relation_type,
                "evidence_version_ref": e["evidence_version_id"],
                "claim_version_ref": c["claim_version_id"], "relation": relation_type,
            })
            self.s.stage_change("change-" + relation_type, thesis_id="t-" + relation_type, content={
                "statement": "s", "mechanism": "m", "confidence": "medium", "implied_expectation": "e",
                "claim_refs": [c["claim_version_id"]], "catalyst_refs": [], "falsifier_refs": [], "change_reason": "test",
            }, producer_invocation=inv("thesis-producer-" + relation_type, "family-a"))
            self.s.verify_change("change-" + relation_type, verification_id="verifier-" + relation_type, verifier_invocation=inv("verifier-" + relation_type, "family-b"), verdict="pass", findings=[])
            self.assertEqual(self.s.commit("change-" + relation_type, "verifier-" + relation_type, "commit-" + relation_type)["status"], "fresh")

    def test_relation_idempotency_is_explicit_and_three_state(self):
        claim = self.claim("idempotent")
        evidence = self.evidence("idempotent-evidence")
        request = {
            "id": "relation-idempotent", "evidence_version_ref": evidence["evidence_version_id"],
            "claim_version_ref": claim["claim_version_id"], "relation": "supports",
        }
        first = self.s.relate_evidence(request, idempotency_key="relation-key")
        self.assertEqual(first["status"], "fresh")
        self.assertEqual(self.s.relate_evidence(request, idempotency_key="relation-key")["status"], "duplicate")
        conflict = dict(request, relation="contradicts")
        self.assertEqual(self.s.relate_evidence(conflict, idempotency_key="relation-key")["status"], "conflict")
        with self.assertRaises(IdempotencyConflict):
            self.s.relate_evidence(dict(request, id="relation-idempotent"))

    def test_adjudication_rejects_future_and_expired_active_policy(self):
        claim = self.claim("policy-window")
        self.s.register_invocation(inv("policy-adjudicator", "family-b"))
        self.s.create_policy({"allowed_verdicts": ["pass"]}, version_number=2,
                             effective_from="2999-01-01T00:00:00+00:00")
        with self.assertRaises(GateRejected):
            self.s.adjudicate_claim({
                "claim_version_ref": claim["claim_version_id"], "adjudicated_status": "corroborated",
                "rationale": "future policy", "adjudicator_invocation_ref": "policy-adjudicator",
            })
        self.s.create_policy({"allowed_verdicts": ["pass"]}, version_number=3,
                             effective_from="2000-01-01T00:00:00+00:00",
                             effective_until="2001-01-01T00:00:00+00:00")
        with self.assertRaises(GateRejected):
            self.s.adjudicate_claim({
                "claim_version_ref": claim["claim_version_id"], "adjudicated_status": "corroborated",
                "rationale": "expired policy", "adjudicator_invocation_ref": "policy-adjudicator",
            })

    def test_claim_requires_existing_producers_and_relation_inherits_evidence(self):
        with self.assertRaises((ValidationError, NotFound)):
            self.s.register_claim({
                "claim_ref": "inline", "subject_ref": "x", "metric_or_aspect": "m", "period": "p",
                "basis": "b", "normalized_statement": "s", "claim_kind": "qualitative",
                "producer_invocation_refs": ["not-registered"], "actor_ref": "actor",
            })
        evidence = self.evidence()
        claim = self.claim()
        with self.assertRaises(ValidationError):
            self.s.relate_evidence({
                "id": "relation-forged-lineage",
                "evidence_version_ref": evidence["evidence_version_id"],
                "claim_version_ref": claim["claim_version_id"], "relation": "supports",
                "source_lineage": ["forged-source"],
            })
        with self.assertRaises(ValidationError):
            self.s.relate_evidence({
                "id": "relation-forged-group",
                "evidence_version_ref": evidence["evidence_version_id"],
                "claim_version_ref": claim["claim_version_id"], "relation": "supports",
                "independence_group": "forged-group",
            })

    def test_versions_cannot_skip_chain_numbers(self):
        first_evidence = self.evidence("chain-e")
        with self.assertRaises(ValidationError):
            self.s.register_evidence({
                "evidence_ref": "chain-e", "version": 99, "source_type": "filing", "source_ref": "sec:chain-e",
                "retrieved_at": "2026-01-01T00:00:00+00:00", "source_lineage": ["sec:chain-e"],
                "independence_group": "sec:chain-e", "actor_ref": "actor",
            })
        with self.assertRaises(ValidationError):
            self.s.register_claim({
                "claim_ref": "chain-c", "version": 99, "subject_ref": "x", "metric_or_aspect": "m", "period": "p",
                "basis": "b", "normalized_statement": "s", "claim_kind": "qualitative",
                "producer_invocation_refs": ["producer-chain-c"], "actor_ref": "actor",
            })
        self.assertEqual(first_evidence["version"], 1)

    def test_adjudication_uses_claim_producers_and_version_order(self):
        claim = self.claim("ordered")
        self.s.register_invocation(inv("adj-ordered-1", "family-b"))
        first = self.s.adjudicate_claim({
            "claim_version_ref": claim["claim_version_id"], "adjudicated_status": "corroborated",
            "rationale": "first", "adjudicator_invocation_ref": "adj-ordered-1",
            "created_at": "2099-01-01T00:00:00+00:00",
        })
        self.assertEqual(first["status"], "corroborated")
        self.s.register_invocation(inv("adj-ordered-2", "family-c"))
        second = self.s.adjudicate_claim({
            "claim_version_ref": claim["claim_version_id"], "adjudicated_status": "retracted",
            "rationale": "second", "adjudicator_invocation_ref": "adj-ordered-2",
            "created_at": "2000-01-01T00:00:00+00:00",
        })
        self.assertEqual(second["status"], "retracted")
        self.assertEqual(self.s.get_claim(claim["claim_version_id"])["status"], "retracted")
        with self.assertRaises(ValidationError):
            self.s.adjudicate_claim({
                "claim_version_ref": claim["claim_version_id"], "version": 9, "adjudicated_status": "contested",
                "rationale": "skip", "adjudicator_invocation_ref": "adj-ordered-2",
            })
        with self.assertRaises(ValidationError):
            self.s.adjudicate_claim({
                "claim_version_ref": claim["claim_version_id"], "adjudicated_status": "contested",
                "rationale": "wrong subject", "adjudicator_invocation_ref": "adj-ordered-2",
                "subject_invocation_refs": ["adj-ordered-1"],
            })


if __name__ == "__main__":
    unittest.main()
