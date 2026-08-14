import json
from dataclasses import MISSING, fields
import unittest
from pathlib import Path

from dalton_core.contracts import (
    CapabilityProposal,
    AdjudicationVersion,
    AdjudicatedStatus,
    ClaimKind,
    ClaimVersion,
    DomainEvent,
    EvidenceRelation,
    EvidenceRelationType,
    EvidenceVersion,
    ExecutionInvocation,
    ExecutionKind,
    GovernancePolicyVersion,
    IndependencePredicate,
    InvocationGranularity,
    ModelInvocation,
    PredicateOperator,
    ResultEnvelope,
    RuntimeProfile,
    ThesisVersion,
    ValidationError,
    ValueKind,
    Verdict,
    VerificationRecord,
    WorkOrder,
    validate_independence_predicate,
)


ROOT = Path(__file__).parents[1]


class ContractTests(unittest.TestCase):
    def test_schema_files_parse_and_are_closed(self):
        schemas = sorted((ROOT / "contracts").glob("*.schema.json"))
        self.assertGreaterEqual(len(schemas), 13)
        for path in schemas:
            with self.subTest(path=path.name):
                schema = json.loads(path.read_text())
                self.assertEqual(schema["type"], "object")
                self.assertFalse(schema["additionalProperties"])
                if "schema_version" not in schema["required"]:
                    # The local writer protocol has its own protocol_version
                    # envelope and is not one of the domain contracts.
                    continue
                self.assertIn("schema_version", schema["required"])
                self.assertIn("id", schema["required"])
                self.assertIn("created_at", schema["required"])
                self.assertTrue(set(schema["required"]).issubset(schema["properties"]))

    def test_json_required_fields_match_python_required_fields(self):
        by_title = {
            cls.__name__: cls
            for cls in (
                WorkOrder, ResultEnvelope, RuntimeProfile, ExecutionInvocation,
                ModelInvocation, DomainEvent,
                CapabilityProposal, ThesisVersion, VerificationRecord,
                GovernancePolicyVersion, EvidenceVersion, ClaimVersion,
                EvidenceRelation, AdjudicationVersion,
            )
        }
        for path in sorted((ROOT / "contracts").glob("*.schema.json")):
            schema = json.loads(path.read_text())
            # Capability-plane schemas may be added by a separate slice before
            # their executable Python contract is promoted into this module.
            if schema["title"] not in by_title:
                continue
            cls = by_title[schema["title"]]
            python_required = {
                item.name for item in fields(cls)
                if item.default is MISSING and item.default_factory is MISSING
            }
            with self.subTest(contract=schema["title"]):
                self.assertEqual(set(schema["required"]), python_required)

    def test_frozen_word_lists(self):
        self.assertEqual(
            {x.value for x in Verdict},
            {"pass", "conditional_pass", "revise", "blocked", "reject"},
        )
        self.assertEqual(
            {x.value for x in ValueKind},
            {"observed", "assumption", "derived_deterministic", "estimate", "simulation"},
        )

    def test_work_and_result_roundtrip(self):
        work = WorkOrder(
            "0.1", "wo-1", "2026-08-13T00:00:00Z", "2026-08-13T00:00:01Z",
            "answer question", ("extract",), "runtime-1", {"tokens": 100}, "idem-1",
            ("filesystem_write",), "ready", ("evidence-1",), {"source": "test"},
        )
        self.assertEqual(WorkOrder.from_dict(work.to_dict()), work)
        result = ResultEnvelope(
            "0.1", "result-1", "2026-08-13T00:00:02Z", "wo-1", "inv-1", "completed",
            {"answer": "ok"}, ("filesystem_write",), ("usage-1",), ("artifact-1",), None,
            {"source": "test"},
        )
        self.assertEqual(ResultEnvelope.from_dict(result.to_dict()), result)

    def test_all_contracts_roundtrip(self):
        profile = RuntimeProfile(
            "0.1", "runtime-1", "2026-08-13T00:00:00Z", "1", ("extract",), "process",
            ("read",), "none", "workspace", (), {"wall_seconds": 10}, ("0.1",),
            ("0.1",), "python-3.11", "sha256:env", {},
        )
        invocation = ModelInvocation(
            "0.1", "inv-1", "2026-08-13T00:00:00Z", "wo-1", "profile-1",
            InvocationGranularity.WORK_ORDER, "extract", "provider-x", "model-x",
            "family-x", ("input-1",), ("output-1",), "2026-08-13T00:00:00Z",
            "2026-08-13T00:00:01Z", {"input_tokens": 1}, (), "runtime-1", "actor-1",
        )
        execution = ExecutionInvocation.from_model(invocation)
        self.assertEqual(execution.kind, ExecutionKind.MODEL)
        self.assertEqual(execution.id, invocation.id)
        with self.assertRaises(ValidationError):
            ExecutionInvocation.from_dict({**execution.to_dict(), "unexpected": True})
        with self.assertRaises(ValidationError):
            ExecutionInvocation.from_dict({**execution.to_dict(), "kind": "unknown"})
        event = DomainEvent(
            "0.1", "event-1", "2026-08-13T00:00:00Z", "staged", "work_order", "wo-1", 1,
            "wo-1:v1", "sha256:wo", "2026-08-13T00:00:00Z", "actor-1", {"status": "ready"},
            "idem-1", "corr-1", None,
        )
        proposal = CapabilityProposal(
            "0.1", "proposal-1", "2026-08-13T00:00:00Z", "2026-08-13T00:00:00Z", "parser",
            "deterministic_tool", {"kind": "gap"}, {"seconds": 10}, {"input": "x"},
            {"network": False}, ("fixture-1",), "f" * 64, {"builder": "actor-1"},
            "proposed", (), None,
        )
        thesis = ThesisVersion(
            "0.1", "thesis-v1", "2026-08-13T00:00:00Z", "thesis-1", 1, "statement",
            "mechanism", 0.7, "expectation", ("claim-1",), (), ("falsifier-1",), "initial",
            None, "verification-1", "actor-1", "sha256:thesis",
        )
        verification = VerificationRecord(
            "0.1", "verification-1", "2026-08-13T00:00:00Z", "result-1", "inv-verify",
            Verdict.PASS, ({"finding": "none"},), ({"check": "ok"},), "script", 0,
            "policy-1", ("inv-1",), "sha256:result",
        )
        policy = GovernancePolicyVersion(
            "0.1", "policy-v1", "2026-08-13T00:00:00Z", "policy-1", 1,
            "2026-08-13T00:00:00Z", None, {"required": True},
            (IndependencePredicate("producer.model_family", PredicateOperator.NE, "verifier.model_family"),),
            "initial", "actor-1", None, "sha256:policy",
        )
        evidence = EvidenceVersion(
            "0.1", "ev-v1", "2026-08-13T00:00:00Z", "ev-1", 1, "filing", "sec:abc",
            "2026-08-13T00:00:00Z", None, ("artifact-1",), ("sec:abc",), "source-1", "actor-1", None, "sha256:ev",
        )
        claim = ClaimVersion(
            "0.1", "cl-v1", "2026-08-13T00:00:00Z", "cl-1", 1, "company:abc", "revenue", "2026Q2", "reported",
            "Revenue was 100", ClaimKind.QUANTITATIVE, 100, "USDm", ("inv-1",), "actor-1", None, "sha256:cl",
        )
        relation = EvidenceRelation(
            "0.1", "rel-1", "2026-08-13T00:00:00Z", "ev-1", "ev-v1", "cl-1", "cl-v1",
            EvidenceRelationType.SUPPORTS, ("sec:abc",), "source-1", "actor-1", "sha256:rel",
        )
        adjudication = AdjudicationVersion(
            "0.1", "adj-1", "2026-08-13T00:00:00Z", "cl-1", "cl-v1", 1, AdjudicatedStatus.CORROBORATED,
            "independent review", ({"finding": "ok"},), "inv-adj", ("inv-subject",), "policy-1", None, "sha256:adj",
        )
        for item in (profile, execution, invocation, event, proposal, thesis, verification, policy, evidence, claim, relation, adjudication):
            with self.subTest(type=type(item).__name__):
                self.assertEqual(type(item).from_dict(item.to_dict()), item)

    def test_unknown_fields_and_required_fields_are_rejected(self):
        sample = {
            "schema_version": "0.1", "id": "event-1", "created_at": "now",
            "event_type": "x", "aggregate_type": "x", "aggregate_id": "x",
            "aggregate_version": 1, "version_ref": "x:v1", "content_hash": "x",
            "occurred_at": "now", "actor_ref": "actor", "payload": {},
            "idempotency_key": "idem", "correlation_id": "corr",
        }
        with self.assertRaises(ValidationError):
            DomainEvent.from_dict({**sample, "unexpected": True})
        missing = dict(sample)
        del missing["content_hash"]
        with self.assertRaises(ValidationError):
            DomainEvent.from_dict(missing)
        required_work = {
            "schema_version": "0.1", "id": "wo", "created_at": "now", "updated_at": "now",
            "question": "q", "requested_capabilities": ["research"],
            "runtime_profile_ref": "runtime", "budget": {"max_tokens": 1},
            "idempotency_key": "idem", "declared_side_effects": [],
        }
        with self.assertRaises(ValidationError):
            WorkOrder.from_dict(required_work)

    def test_staged_event_can_precede_version(self):
        event = DomainEvent(
            "0.1", "event-staged", "now", "staged", "work_order", "wo-1", 0,
            None, "sha256:staged", "now", "actor", {}, "idem", "corr",
        )
        self.assertIsNone(event.version_ref)

    def test_invalid_enum_is_rejected(self):
        with self.assertRaises((ValidationError, ValueError)):
            Verdict("not-a-verdict")
        with self.assertRaises(ValidationError):
            VerificationRecord.from_dict({
                "schema_version": "0.1", "id": "v", "created_at": "now", "target_ref": "r",
                "target_content_hash": "h", "verifier_invocation_ref": "i", "verdict": "bad",
                "findings": [], "deterministic_checks": [], "verifier_kind": "script",
                "revise_round": 0, "independence_policy_ref": "p", "subject_invocation_refs": [],
            })
        with self.assertRaises(ValidationError):
            ThesisVersion(
                "0.1", "t-v", "now", "t", 1, "s", "m", True, "e", (), (), (),
                "reason", None, "verify", "actor", "hash",
            )

    def test_independence_predicate_closed_shape(self):
        pair = validate_independence_predicate({
            "left_path": "producer.model_family", "operator": "ne",
            "right_path": "verifier.model_family",
        })
        self.assertEqual(pair.to_dict()["right_path"], "verifier.model_family")
        for operator in ("eq", "ne"):
            self.assertEqual(validate_independence_predicate({
                "left_path": "producer.provider", "operator": operator, "value": "policy-provider",
            }).operator.value, operator)
        for operator in ("in", "not_in"):
            validate_independence_predicate({
                "left_path": "producer.provider", "operator": operator, "value": ["a", "b"],
            })
        bad = [
            {"left_path": "invocation.provider", "operator": "eq", "value": "x"},
            {"left_path": "producer.provider", "operator": "xor", "value": "x"},
            {"left_path": "producer.provider", "operator": "in", "value": []},
            {"left_path": "producer.provider", "operator": "eq", "right_path": "verifier.provider", "value": "x"},
            {"left_path": "producer.provider", "operator": "eq", "value": "x", "extra": 1},
        ]
        for predicate in bad:
            with self.subTest(predicate=predicate), self.assertRaises((ValidationError, ValueError)):
                validate_independence_predicate(predicate)


if __name__ == "__main__":
    unittest.main()
