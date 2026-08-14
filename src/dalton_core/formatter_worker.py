"""Stdlib-only deterministic formatter process fixture.

The fixture canonicalizes ``work_order.metadata.formatter_records`` into
JSON Lines.  It intentionally has no Core-store import and only speaks the
process envelope; the parent adapter performs the authoritative contract
checks.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from typing import Any


def formatter_worker_command() -> list[str]:
    return [sys.executable, os.path.abspath(__file__)]


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _stable_id(prefix: str, value: Any) -> str:
    return f"{prefix}-{hashlib.sha256(_canonical(value).encode('utf-8')).hexdigest()[:32]}"


def _main() -> int:
    line = sys.stdin.buffer.readline()
    if not line:
        return 2
    try:
        request = json.loads(line.decode("utf-8"))
        if not isinstance(request, dict) or set(request) != {"protocol_version", "work_order", "runtime_profile"}:
            raise ValueError("invalid request envelope")
        if request["protocol_version"] != "0.1":
            raise ValueError("unsupported protocol version")
        work = request["work_order"]
        profile = request["runtime_profile"]
        if not isinstance(work, dict) or not isinstance(profile, dict):
            raise ValueError("work_order and runtime_profile must be objects")
        records = work.get("metadata", {}).get("formatter_records", [])
        if not isinstance(records, list) or not all(isinstance(item, dict) for item in records):
            raise ValueError("formatter_records must be a list of objects")
        lines = [_canonical(item) for item in records]
        formatted = "\n".join(lines) + ("\n" if lines else "")
        artifact_hash = hashlib.sha256(formatted.encode("utf-8")).hexdigest()
        invocation_id = _stable_id(
            "inv",
            {"work": work["id"], "profile": profile["id"], "records": records},
        )
        artifact_ref = f"artifact:formatter:{artifact_hash}"
        usage_ref = f"usage:{invocation_id}"
        invocation = {
            "schema_version": work["schema_version"],
            "id": invocation_id,
            "created_at": work["created_at"],
            "work_order_ref": work["id"],
            "profile_ref": profile["id"],
            "granularity": "task",
            "capability": work["requested_capabilities"][0],
            "provider": "dalton-fixture",
            "model": "deterministic-formatter-v1",
            "model_family": "dalton-deterministic",
            "input_refs": work.get("input_refs", []),
            "output_refs": [artifact_ref],
            "started_at": work["created_at"],
            "completed_at": work["created_at"],
            "usage": {
                "records": len(records),
                "input_bytes": len(_canonical(records).encode("utf-8")),
                "output_bytes": len(formatted.encode("utf-8")),
            },
            "side_effects": [],
            "runtime_ref": profile["id"],
            "actor_ref": "fixture:formatter",
            "parent_ref": None,
            "environment_hash": profile["environment_hash"],
        }
        result = {
            "schema_version": work["schema_version"],
            "id": _stable_id("result", {"invocation": invocation_id, "artifact": artifact_hash}),
            "created_at": work["created_at"],
            "work_order_ref": work["id"],
            "invocation_ref": invocation_id,
            "status": "completed",
            "outputs": {
                "format": "canonical-jsonl-v1",
                "formatted": formatted,
                "record_count": len(records),
            },
            "actual_side_effects": [],
            "usage_refs": [usage_ref],
            "artifact_refs": [artifact_ref],
            "error": None,
            "metadata": {
                "worker": "deterministic-formatter",
                "provenance": {
                    "input_ref": work["id"],
                    "environment_hash": profile["environment_hash"],
                },
            },
        }
        response = {"protocol_version": "0.1", "invocation": invocation, "result": result}
        sys.stdout.write(_canonical(response) + "\n")
        sys.stdout.flush()
        return 0
    except Exception as exc:
        # Error text goes to stderr; stdout remains a single-frame protocol.
        sys.stderr.write(f"formatter fixture error: {exc}\n")
        sys.stderr.flush()
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())


__all__ = ["formatter_worker_command"]
