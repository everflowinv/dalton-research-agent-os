#!/usr/bin/env python3
"""Prepare or explicitly install the reviewed SEC 8-K discovery selection."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from build_sec_8k_discovery_proposal import (  # noqa: E402
    _next_plan_id,
    build_review_bundle,
    read_active_mission,
    verify_mission_authority_on_backup,
)
from dalton_core.macos_launchagent import SEC_PLAN_SELECTOR  # noqa: E402
from dalton_core.mission_source_discovery import (  # noqa: E402
    SEC_SOURCE_REF,
    validate_discovery_plan,
)
from dalton_core.store import content_hash  # noqa: E402

APPROVAL_RECEIPT = "sec-filings-plan-selection-v1.approval.json"
SELECTOR_SCHEMA = "sec-discovery-plan-selection-0.1"
RECEIPT_SCHEMA = "sec-discovery-selection-approval-0.1"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def checked(path: Path, expected: str) -> dict[str, Any]:
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError(f"sha256 mismatch: {path}")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


def _approved_selector(selector: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    expected = {
        "schema_version", "id", "status", "source_ref", "plan_ref",
        "plan_hash", "plan_path", "content_hash",
    }
    body = {key: value for key, value in selector.items() if key != "content_hash"}
    if set(selector) != expected or selector.get("status") != "proposed":
        raise ValueError("selector must be the closed proposed record")
    if selector["content_hash"] != content_hash(body):
        raise ValueError("selector content hash drifted")
    if (selector["schema_version"] != SELECTOR_SCHEMA
            or selector["source_ref"] != SEC_SOURCE_REF):
        raise ValueError("selector schema/source is invalid")
    if (selector["plan_ref"] != candidate["id"]
            or selector["plan_hash"] != candidate["content_hash"]):
        raise ValueError("selector does not bind candidate ref/hash")
    relative = Path(selector["plan_path"])
    if relative.is_absolute() or len(relative.parts) != 1:
        raise ValueError("selector plan_path must be a plain filename")
    if relative.name in {SEC_PLAN_SELECTOR, APPROVAL_RECEIPT}:
        raise ValueError("candidate, receipt, and selector targets must be distinct")
    approved = {**selector, "status": "approved"}
    approved["content_hash"] = content_hash(
        {key: value for key, value in approved.items() if key != "content_hash"}
    )
    return approved


def validate_packet(args: argparse.Namespace) -> dict[str, Any]:
    sources = [args.active_plan, args.candidate, args.selector, args.governance]
    if len({path.resolve() for path in sources}) != len(sources):
        raise ValueError("source artifacts must be distinct")
    active = validate_discovery_plan(
        checked(args.active_plan, args.active_plan_sha256)
    )
    if active["content_hash"] != args.active_plan_hash:
        raise ValueError("active plan original hash changed")
    candidate = validate_discovery_plan(
        checked(args.candidate, args.candidate_sha256)
    )
    if candidate["id"] != _next_plan_id(active["id"]):
        raise ValueError("candidate must be the next active plan version")
    preserved = set(active) - {"id", "created_at", "content_hash", "specs"}
    if any(candidate.get(key) != active.get(key) for key in preserved):
        raise ValueError("candidate changes preserved plan scope")
    approved_selector = _approved_selector(
        checked(args.selector, args.selector_sha256), candidate
    )
    governance = checked(args.governance, args.governance_sha256)
    mission = read_active_mission(args.source_core, active["mission_ref"])
    build_review_bundle(
        active_plan=active,
        candidate_plan=candidate,
        mission=mission,
        governance=governance,
        created_at="1970-01-01T00:00:00+00:00",
    )
    verify_mission_authority_on_backup(args.source_core, mission)
    return {
        "active": active,
        "candidate": candidate,
        "approved_selector": approved_selector,
        "mission": mission,
        "governance": governance,
    }


def encoded(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def state(path: Path, data: bytes) -> str:
    if path.is_symlink():
        return "conflict"
    if not path.exists():
        return "absent"
    if path.is_file() and path.read_bytes() == data:
        return "identical"
    return "conflict"


def atomic_create(path: Path, data: bytes) -> str:
    current = state(path, data)
    if current == "identical":
        return "identical"
    if current == "conflict":
        raise FileExistsError(f"refusing to overwrite different target: {path}")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(name, 0o600)
        os.link(name, path)
    finally:
        Path(name).unlink(missing_ok=True)
    return "created"


def receipt_body(
    packet: dict[str, Any], actor: str, approved_at: str
) -> dict[str, Any]:
    body = {
        "schema_version": RECEIPT_SCHEMA,
        "actor_ref": actor,
        "approved_at": approved_at,
        "mission_binding": {
            "ref": packet["mission"]["id"],
            "hash": packet["mission"]["content_hash"],
        },
        "prior_plan": {
            "ref": packet["active"]["id"],
            "hash": packet["active"]["content_hash"],
        },
        "candidate": {
            "ref": packet["candidate"]["id"],
            "hash": packet["candidate"]["content_hash"],
        },
        "selector": {
            "ref": packet["approved_selector"]["id"],
            "hash": packet["approved_selector"]["content_hash"],
        },
        "governance_binding": {
            "ref": packet["governance"]["id"],
            "hash": packet["governance"]["content_hash"],
        },
    }
    return {**body, "content_hash": content_hash(body)}


def existing_receipt(path: Path, packet: dict[str, Any]) -> dict[str, Any] | None:
    if path.is_symlink():
        raise FileExistsError(f"refusing symlink target: {path}")
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != RECEIPT_SCHEMA:
        raise FileExistsError(f"different approval receipt already exists: {path}")
    if value.get("content_hash") != content_hash(
        {key: item for key, item in value.items() if key != "content_hash"}
    ):
        raise FileExistsError(f"approval receipt hash drifted: {path}")
    actor = value.get("actor_ref")
    approved_at = value.get("approved_at")
    if not isinstance(actor, str) or not actor.startswith("human:"):
        raise FileExistsError(f"approval receipt actor is invalid: {path}")
    expected = receipt_body(packet, actor, approved_at)
    if value != expected:
        raise FileExistsError(f"approval receipt binds a different packet: {path}")
    return value


def run(args: argparse.Namespace) -> dict[str, Any]:
    packet = validate_packet(args)
    target_plan = args.target_dir / packet["approved_selector"]["plan_path"]
    target_selector = args.target_dir / SEC_PLAN_SELECTOR
    target_receipt = args.target_dir / APPROVAL_RECEIPT
    targets = (target_plan, target_receipt, target_selector)
    if len({path.resolve() for path in targets}) != 3:
        raise ValueError("candidate, receipt, and selector targets must be distinct")
    if {path.resolve() for path in targets} & {
        args.active_plan.resolve(), args.candidate.resolve(), args.selector.resolve(),
        args.governance.resolve(), args.source_core.resolve(),
    }:
        raise ValueError("source artifacts may not also be install targets")

    prior_receipt = existing_receipt(target_receipt, packet)
    if args.command == "apply":
        if not args.execute or not args.service_stopped_ack:
            raise ValueError("apply requires --execute and --service-stopped-ack")
        if not isinstance(args.actor, str) or not args.actor.startswith("human:"):
            raise ValueError("apply requires a human: approval actor")
        if prior_receipt is not None and prior_receipt["actor_ref"] != args.actor:
            raise FileExistsError("approval receipt belongs to a different actor")
        approved_at = (
            prior_receipt["approved_at"] if prior_receipt is not None
            else datetime.now(timezone.utc).isoformat(timespec="microseconds")
        )
        receipt = receipt_body(packet, args.actor, approved_at)
        receipt_bytes = encoded(receipt)
    else:
        receipt_bytes = encoded(prior_receipt) if prior_receipt is not None else None

    plan_bytes = encoded(packet["candidate"])
    selector_bytes = encoded(packet["approved_selector"])
    states = {
        "candidate": state(target_plan, plan_bytes),
        "selector": state(target_selector, selector_bytes),
        "approval_receipt": (
            state(target_receipt, receipt_bytes) if receipt_bytes is not None else "absent"
        ),
    }
    conflicts = [name for name, value in states.items() if value == "conflict"]
    if conflicts:
        raise FileExistsError("different target already exists: " + ",".join(conflicts))
    report = {
        "mode": args.command,
        "actor_ref": args.actor,
        "mission_binding": {
            "ref": packet["mission"]["id"], "hash": packet["mission"]["content_hash"]
        },
        "prior_plan": {
            "ref": packet["active"]["id"], "hash": packet["active"]["content_hash"]
        },
        "candidate": {
            "ref": packet["candidate"]["id"], "hash": packet["candidate"]["content_hash"],
            "target": str(target_plan), "target_state": states["candidate"],
        },
        "selector": {
            "ref": packet["approved_selector"]["id"],
            "hash": packet["approved_selector"]["content_hash"],
            "target": str(target_selector), "target_state": states["selector"],
        },
        "approval_receipt": {
            "target": str(target_receipt),
            "target_state": states["approval_receipt"],
            "actor_ref": None if prior_receipt is None else prior_receipt["actor_ref"],
            "approved_at": None if prior_receipt is None else prior_receipt["approved_at"],
        },
        "delta": {
            "added_specs": [packet["candidate"]["specs"][-1]],
            "preserved_companies": sorted(packet["candidate"]["companies"]),
            "budget": packet["candidate"]["budget"],
        },
        "requires_reinstall": True,
        "artifact_acceptance": False,
    }
    if args.command == "apply":
        report["candidate"]["write"] = atomic_create(target_plan, plan_bytes)
        report["approval_receipt"]["write"] = atomic_create(
            target_receipt, receipt_bytes
        )
        report["selector"]["write"] = atomic_create(target_selector, selector_bytes)
        report["result"] = "installed_for_next_render; run install/re-render before activation"
    else:
        report["result"] = "prepared_only; no files written"
    return report


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("command", choices=("prepare", "apply"))
    result.add_argument("--active-plan", type=Path, required=True)
    result.add_argument("--active-plan-hash", required=True)
    result.add_argument("--active-plan-sha256", required=True)
    result.add_argument("--candidate", type=Path, required=True)
    result.add_argument("--candidate-sha256", required=True)
    result.add_argument("--selector", type=Path, required=True)
    result.add_argument("--selector-sha256", required=True)
    result.add_argument("--governance", type=Path, required=True)
    result.add_argument("--governance-sha256", required=True)
    result.add_argument("--source-core", type=Path, required=True)
    result.add_argument("--target-dir", type=Path, required=True)
    result.add_argument("--actor")
    result.add_argument("--execute", action="store_true")
    result.add_argument("--service-stopped-ack", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    print(json.dumps(run(args), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
