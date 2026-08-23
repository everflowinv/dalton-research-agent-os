"""Child-process fixture: die after version insertion; SQLite must roll back."""
import os
import sys

from dalton_core.store import DaltonStore


def main(path: str) -> None:
    store = DaltonStore(path)
    def invocation(i):
        return {"schema_version": "0.1", "id": i, "created_at": "2026-01-01T00:00:00+00:00", "work_order_ref": "wo", "profile_ref": "profile-" + i,
                "granularity": "task", "capability": "research", "provider": i,
                "model": "model-" + i, "model_family": i, "runtime_ref": "runtime",
                "actor_ref": "actor", "usage": {"tokens": 1}, "input_refs": [], "output_refs": [],
                "started_at": "2026-01-01T00:00:00+00:00", "completed_at": None, "side_effects": [], "parent_ref": None}
    store.stage_change("crash-change", thesis_id="crash-thesis", content={"statement": "s", "mechanism": "m", "confidence": "medium", "implied_expectation": "e", "claim_refs": [], "catalyst_refs": [], "falsifier_refs": [], "change_reason": "crash"}, producer_invocation=invocation("crash-producer"))
    store.verify_change("crash-change", verification_id="crash-verification", verifier_invocation=invocation("crash-verifier"), verdict="pass")
    original = store._insert_event

    def crash(*args, **kwargs):
        os._exit(17)

    store._insert_event = crash
    store.commit("crash-change", "crash-verification", "crash-key")
    original  # pragma: no cover


if __name__ == "__main__":
    main(sys.argv[1])
