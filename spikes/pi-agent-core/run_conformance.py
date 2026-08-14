"""Run the pinned Pi worker through Dalton's strict process adapter."""

from __future__ import annotations

import json
from pathlib import Path
import shutil

from dalton_core.contracts import RuntimeProfile, WorkOrder
from dalton_core.process_runtime import ProcessRuntimeAdapter


TIME = "2026-01-01T00:00:00Z"


def main() -> int:
    node = shutil.which("node")
    if node is None:
        raise SystemExit("node is required")
    worker = Path(__file__).with_name("worker.mjs").resolve()
    profile = RuntimeProfile(
        schema_version="0.1",
        id="runtime:pi-agent-core-0.84.1",
        created_at=TIME,
        version="0.84.1",
        capabilities=("format.records",),
        isolation_level="temp-process",
        allowed_tools=("format_records",),
        network="disabled",
        filesystem="temp",
        side_effects=(),
        limits={"max_tokens": 64, "max_seconds": 5},
        supported_input_versions=("0.1",),
        supported_result_versions=("0.1",),
        runtime_version="0.84.1",
        environment_hash="env:pi-spike",
        metadata={
            "invocation_identity": {
                "provider": "local-fixture",
                "model": "dalton-pi-spike",
                "model_family": "pi-agent-core-fixture",
                "actor_ref": "runtime:pi-agent-core",
            }
        },
    )
    work = WorkOrder(
        schema_version="0.1",
        id="wo-pi-spike-001",
        created_at=TIME,
        updated_at=TIME,
        question="format records",
        requested_capabilities=("format.records",),
        runtime_profile_ref=profile.id,
        budget={"max_tokens": 64, "max_seconds": 5},
        idempotency_key="idem:wo-pi-spike-001",
        declared_side_effects=(),
        status="ready",
        input_refs=("artifact:fixture-1",),
        metadata={"formatter_records": [{"z": 2, "a": "x"}, {"a": 1}]},
    )
    invocation, result = ProcessRuntimeAdapter(
        (node, str(worker)),
        invocation_identity={
            "provider": "local-fixture",
            "model": "dalton-pi-spike",
            "model_family": "pi-agent-core-fixture",
            "actor_ref": "runtime:pi-agent-core",
        },
    ).execute(work, profile)
    print(json.dumps({"invocation": invocation.to_dict(), "result": result.to_dict()}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
