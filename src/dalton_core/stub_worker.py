"""Deterministic producer/verifier fixtures for the Dalton Core slice."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Mapping

from .contracts import RuntimeProfile, WorkOrder
from .executor import LocalDeterministicExecutor


def _now_fallback() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class StubModelWorker:
    """A controllable two-role worker that never calls a model.

    ``same_invocation`` and ``same_model_family`` deliberately provide bad
    fixtures for testing the commit gate.  ``producer_fixture`` and
    ``verifier_fixture`` are merged into the respective deterministic result,
    making it possible to test output and usage handling without an LLM.
    """

    def __init__(
        self,
        executor: LocalDeterministicExecutor | None = None,
        *,
        producer_capability: str = "stub.produce",
        verifier_capability: str = "stub.verify",
        producer_model_family: str = "stub-producer",
        verifier_model_family: str = "stub-verifier",
        same_invocation: bool = False,
        same_model_family: bool = False,
        producer_family: str | None = None,
        verifier_family: str | None = None,
        same_family: bool | None = None,
        producer_fixture: Mapping[str, Any] | None = None,
        verifier_fixture: Mapping[str, Any] | None = None,
    ) -> None:
        self.executor = executor or LocalDeterministicExecutor()
        self.producer_capability = producer_capability
        self.verifier_capability = verifier_capability
        self.producer_model_family = producer_family or producer_model_family
        self.verifier_model_family = verifier_family or verifier_model_family
        self.same_invocation = same_invocation
        self.same_model_family = same_model_family if same_family is None else same_family
        self.producer_fixture = dict(producer_fixture or {})
        self.verifier_fixture = dict(verifier_fixture or {})
        self.executor.register(producer_capability, self._produce_handler)
        self.executor.register(verifier_capability, self._verify_handler)

    @staticmethod
    def _root(work: WorkOrder) -> str:
        return str(work.metadata.get("chain_root", work.id))

    def _invocation_fixture(self, work: WorkOrder, *, role: str, family: str) -> dict[str, Any]:
        root = self._root(work)
        invocation_id = f"stub-{root}-{role}" if not self.same_invocation else f"stub-{root}-shared"
        return {
            "id": invocation_id,
            "provider": "stub",
            "model": f"{family}-model",
            "model_family": family,
            "actor_ref": f"stub-worker:{role}",
        }

    def _produce_handler(self, work: WorkOrder, profile: RuntimeProfile) -> Mapping[str, Any]:
        result = {
            "outputs": {"statement": work.question, "mechanism": "deterministic fixture", "confidence": "medium",
                        "implied_expectation": "fixture completes", "claim_refs": [], "catalyst_refs": [],
                        "falsifier_refs": [], "change_reason": "stub producer"},
            "usage": {"input_tokens": 1, "output_tokens": 1, "tokens": 2},
            "status": "completed",
            "_invocation": self._invocation_fixture(work, role="producer", family=self.producer_model_family),
        }
        result.update(self.producer_fixture)
        return result

    def _verify_handler(self, work: WorkOrder, profile: RuntimeProfile) -> Mapping[str, Any]:
        family = self.producer_model_family if self.same_model_family else self.verifier_model_family
        result = {
            "outputs": {"verdict": "pass", "findings": [{"check": "deterministic", "ok": True}]},
            "usage": {"input_tokens": 1, "output_tokens": 1, "tokens": 2},
            "status": "completed",
            "_invocation": self._invocation_fixture(work, role="verifier", family=family),
        }
        result.update(self.verifier_fixture)
        return result

    def produce(self, work_order: WorkOrder | Mapping[str, Any], runtime_profile: RuntimeProfile | Mapping[str, Any]):
        return self.executor.execute(work_order, runtime_profile)

    run_producer = produce

    def verify(self, work_order: WorkOrder | Mapping[str, Any], runtime_profile: RuntimeProfile | Mapping[str, Any]):
        return self.executor.execute(work_order, runtime_profile)

    run_verifier = verify

    @staticmethod
    def _as_work(value: WorkOrder | Mapping[str, Any]) -> WorkOrder:
        if isinstance(value, WorkOrder):
            return value
        return WorkOrder.from_dict(value)

    def _verifier_work(self, producer_work: WorkOrder) -> WorkOrder:
        metadata = dict(producer_work.metadata)
        metadata["chain_root"] = self._root(producer_work)
        metadata["phase"] = "verify"
        return replace(
            producer_work,
            id=f"{producer_work.id}:verify",
            updated_at=producer_work.updated_at or _now_fallback(),
            requested_capabilities=(self.verifier_capability,),
            metadata=metadata,
        )

    def run_chain(
        self,
        store: Any,
        work_order: WorkOrder | Mapping[str, Any],
        runtime_profile: RuntimeProfile | Mapping[str, Any],
        *,
        thesis_id: str = "stub-thesis",
        change_id: str = "stub-change",
        verification_id: str = "stub-verification",
        idempotency_key: str = "stub-commit",
        content: Any = None,
        actor_id: str = "stub-worker",
    ) -> dict[str, Any]:
        work = self._as_work(work_order)
        producer_invocation, producer_result = self.produce(work, runtime_profile)
        staged_content = content if content is not None else producer_result.outputs
        stage = store.stage_change(
            change_id,
            thesis_id=thesis_id,
            content=staged_content,
            producer_invocation=producer_invocation.to_dict(),
            actor_id=actor_id,
        )
        verifier_work = self._verifier_work(work)
        verifier_invocation, verifier_result = self.verify(verifier_work, runtime_profile)
        verdict = str(verifier_result.outputs.get("verdict", "pass"))
        findings = verifier_result.outputs.get("findings", verifier_result.outputs)
        # A same-invocation fixture is intentionally passed as an immutable
        # reference.  Supplying the second full envelope would be rejected as
        # a conflicting reuse before the governance gate can report the real
        # independence failure.
        verifier_reference: Any = verifier_invocation.to_dict()
        if self.same_invocation:
            verifier_reference = {"invocation_id": verifier_invocation.id}
        verification = store.verify_change(
            change_id,
            verification_id=verification_id,
            verifier_invocation=verifier_reference,
            verdict=verdict,
            findings=findings,
            actor_id=actor_id,
        )
        commit = store.commit(change_id, verification_id, idempotency_key, actor_id=actor_id)
        return {
            "stage": stage,
            "verification": verification,
            "commit": commit,
            "producer_invocation": producer_invocation.to_dict(),
            "producer_result": producer_result.to_dict(),
            "verifier_invocation": verifier_invocation.to_dict(),
            "verifier_result": verifier_result.to_dict(),
        }

    stage_verify_commit = run_chain
    execute_chain = run_chain
    run = run_chain


__all__ = ["StubModelWorker"]
