"""Offline walking skeleton: stage, verify, and commit one thesis change."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from typing import Any

from .contracts import RuntimeProfile, WorkOrder
from .store import DaltonStore
from .stub_worker import StubModelWorker


DEMO_TIME = "2026-01-01T00:00:00Z"


def build_demo_work_order() -> WorkOrder:
    return WorkOrder(
        schema_version="0.1",
        id="demo-work-order",
        created_at=DEMO_TIME,
        updated_at=DEMO_TIME,
        question="Can the deterministic fixture commit a thesis?",
        requested_capabilities=("stub.produce",),
        runtime_profile_ref="demo-runtime-profile",
        budget={"max_tokens": 100},
        idempotency_key="demo-commit",
        declared_side_effects=(),
        status="ready",
        input_refs=("demo-input",),
        metadata={"chain_root": "demo"},
    )


def build_demo_profile() -> RuntimeProfile:
    return RuntimeProfile(
        schema_version="0.1",
        id="demo-runtime-profile",
        created_at=DEMO_TIME,
        version="1",
        capabilities=("stub.produce", "stub.verify"),
        isolation_level="in-process-deterministic",
        allowed_tools=(),
        network="disabled",
        filesystem="none",
        side_effects=(),
        limits={"max_tokens": 100},
        supported_input_versions=("0.1",),
        supported_result_versions=("0.1",),
        runtime_version="dalton-core-stub-1",
        environment_hash="sha256:dalton-core-stub",
    )


def run_demo(db_path: str | Path = ":memory:") -> dict[str, Any]:
    with DaltonStore(db_path) as store:
        worker = StubModelWorker()
        return worker.run_chain(
            store,
            build_demo_work_order(),
            build_demo_profile(),
            thesis_id="demo-thesis",
            change_id="demo-change",
            verification_id="demo-verification",
            idempotency_key="demo-commit",
            actor_id="demo",
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", metavar="PATH", help="blank SQLite path (default: temporary database)")
    args = parser.parse_args(argv)
    if args.db:
        result = run_demo(args.db)
    else:
        with tempfile.TemporaryDirectory(prefix="dalton-demo-") as directory:
            result = run_demo(Path(directory) / "demo.sqlite")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["build_demo_profile", "build_demo_work_order", "main", "run_demo"]
