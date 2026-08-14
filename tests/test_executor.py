import unittest

from dalton_core.contracts import RuntimeProfile, WorkOrder
from dalton_core.errors import IndependenceViolation
from dalton_core.executor import (
    BudgetRejected,
    CapabilityRejected,
    ExecutionRejected,
    LocalDeterministicExecutor,
    SideEffectEscalation,
)
from dalton_core.store import DaltonStore
from dalton_core.stub_worker import StubModelWorker


TIME = "2026-01-01T00:00:00Z"


def profile(*caps, side_effects=(), limits=None, input_versions=("0.1",), result_versions=("0.1",)):
    return RuntimeProfile(
        schema_version="0.1", id="profile-1", created_at=TIME, version="1",
        capabilities=tuple(caps), isolation_level="test", allowed_tools=(),
        network="disabled", filesystem="none", side_effects=tuple(side_effects),
        limits=dict(limits or {}), supported_input_versions=tuple(input_versions),
        supported_result_versions=tuple(result_versions), runtime_version="test", environment_hash="env",
    )


def work(cap="answer", declared=(), metadata=None):
    return WorkOrder(
        schema_version="0.1", id="work-1", created_at=TIME, updated_at=TIME,
        question="question", requested_capabilities=(cap,), runtime_profile_ref="profile-1",
        budget={"max_tokens": 10}, idempotency_key="idempotent-1",
        declared_side_effects=tuple(declared), status="ready", metadata=dict(metadata or {}),
    )


class ExecutorTests(unittest.TestCase):
    def test_deterministic_success(self):
        executor = LocalDeterministicExecutor({"answer": lambda _w, _p: {"outputs": {"answer": 42}, "usage": {"tokens": 1}}})
        first = executor.execute(work(), profile("answer"))
        second = executor.execute(work(), profile("answer"))
        self.assertEqual(first[0].to_dict(), second[0].to_dict())
        self.assertEqual(first[1].to_dict(), second[1].to_dict())
        self.assertEqual(first[1].outputs["answer"], 42)
        self.assertTrue(first[0].created_at.endswith("Z"))

    def test_capability_reject(self):
        executor = LocalDeterministicExecutor()
        with self.assertRaises(CapabilityRejected):
            executor.execute(work(), profile("other"))

    def test_side_effect_escalation_reject(self):
        executor = LocalDeterministicExecutor({"answer": lambda _w, _p: {"outputs": {}, "side_effects": ["network"]}})
        with self.assertRaises(SideEffectEscalation):
            executor.execute(work(), profile("answer", side_effects=("network",)))

    def test_profile_limits_and_versions_are_enforced(self):
        executor = LocalDeterministicExecutor({"answer": lambda _w, _p: {"outputs": {}, "usage": {"tokens": 2}}})
        with self.assertRaises(BudgetRejected):
            executor.execute(work(), profile("answer", limits={"max_tokens": 1}))
        with self.assertRaises(ExecutionRejected):
            executor.execute(work(), profile("answer", input_versions=("9.9",)))
        with self.assertRaises(ExecutionRejected):
            executor.execute(work(), profile("answer", result_versions=("9.9",)))

    def test_reference_fields_are_not_coerced(self):
        executor = LocalDeterministicExecutor({"answer": lambda _w, _p: {"outputs": {}, "output_refs": [7]}})
        with self.assertRaises(ExecutionRejected):
            executor.execute(work(), profile("answer"))

    def test_complete_success_chain(self):
        worker = StubModelWorker()
        with DaltonStore(":memory:") as store:
            store.create_policy(
                {"required_verification": True, "allowed_verdicts": ["pass"],
                 "independence_predicates": [{"left_path": "producer.model_family", "operator": "ne", "right_path": "verifier.model_family"}]},
                policy_version_id="policy-test-success", version_number=2, activate=True,
            )
            result = worker.run_chain(store, work("stub.produce"), profile("stub.produce", "stub.verify"), thesis_id="t")
            self.assertEqual(result["commit"]["status"], "fresh")
            self.assertEqual(store.current_pointer("t")["version_number"], 1)

    def test_same_invocation_reject(self):
        worker = StubModelWorker(same_invocation=True)
        with DaltonStore(":memory:") as store:
            with self.assertRaises(IndependenceViolation):
                worker.run_chain(store, work("stub.produce"), profile("stub.produce", "stub.verify"))

    def test_same_family_policy_reject(self):
        worker = StubModelWorker(same_model_family=True)
        with DaltonStore(":memory:") as store:
            store.create_policy(
                {"required_verification": True, "allowed_verdicts": ["pass"],
                 "independence_predicates": [{"left_path": "producer.model_family", "operator": "ne", "right_path": "verifier.model_family"}]},
                policy_version_id="policy-test-2", version_number=2, activate=True,
            )
            with self.assertRaises(IndependenceViolation):
                worker.run_chain(store, work("stub.produce"), profile("stub.produce", "stub.verify"))


if __name__ == "__main__":
    unittest.main()
