#!/usr/bin/env python3
"""Build, but never publish, the next SEC discovery plan with an 8-K spec."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dalton_core.connector_governance import (
    ConnectorGovernance, SEC_FILINGS_INDEX_CAPABILITY_ID, build_governance_record,
)  # noqa: E402
from dalton_core.coverage_mission import CoverageMissionAuthority, validate_coverage_mission_version  # noqa: E402
from dalton_core.mission_source_discovery import (  # noqa: E402
    SEC_SOURCE_REF,
    load_discovery_plan,
    validate_discovery_plan,
)
from dalton_core.store import DaltonStore, content_hash  # noqa: E402

DEFAULT_SPEC = {
    "spec_ref": "current-report-8k",
    "form": "8-K",
    "lookback_days": 30,
    "rediscovery_interval_days": 1,
    "retry_interval_days": 1,
}


def _next_plan_id(plan_id: str) -> str:
    prefix, separator, suffix = plan_id.rpartition(":")
    if not separator or not suffix.isdigit():
        raise ValueError("active SEC discovery plan id must end in a numeric version")
    return f"{prefix}:{int(suffix) + 1}"


def build_candidate_plan(
    active_plan: Mapping[str, Any], *, created_at: str, spec: Mapping[str, Any] = DEFAULT_SPEC,
) -> dict[str, Any]:
    """Clone an active SEC plan and append one 8-K spec without changing policy."""

    active = validate_discovery_plan(active_plan)
    if active["source_ref"] != SEC_SOURCE_REF:
        raise ValueError("active plan is not the SEC filings discovery plan")
    if spec.get("form") != "8-K":
        raise ValueError("this proposal builder only adds form 8-K")
    if any(row["form"].upper() == "8-K" for row in active["specs"]):
        raise ValueError("active SEC discovery plan already contains an 8-K spec")
    if any(row["spec_ref"] == spec.get("spec_ref") for row in active["specs"]):
        raise ValueError("active SEC discovery plan already contains the proposed spec_ref")
    body = {
        key: json.loads(json.dumps(value))
        for key, value in active.items() if key != "content_hash"
    }
    body["id"] = _next_plan_id(active["id"])
    parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("created_at must include a timezone")
    body["created_at"] = parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")
    body["specs"].append(dict(spec))
    return validate_discovery_plan({**body, "content_hash": content_hash(body)})


def read_active_mission(source_core: Path, mission_ref: str) -> dict[str, Any]:
    connection = sqlite3.connect(f"file:{source_core}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT v.record_json,v.content_hash FROM coverage_mission_pointer p "
            "JOIN coverage_mission_versions v ON v.mission_version_id=p.mission_version_id "
            "WHERE p.mission_ref=?", (mission_ref,),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise RuntimeError(f"no active mission for {mission_ref}")
    mission = validate_coverage_mission_version(json.loads(row["record_json"]))
    if mission["content_hash"] != row["content_hash"]:
        raise RuntimeError("active mission row hash drifted")
    return mission


def build_review_bundle(
    *, active_plan: Mapping[str, Any], candidate_plan: Mapping[str, Any],
    mission: Mapping[str, Any], governance: Mapping[str, Any], created_at: str,
) -> dict[str, Any]:
    active = validate_discovery_plan(active_plan)
    candidate = validate_discovery_plan(candidate_plan)
    approved = ConnectorGovernance(governance)
    expected = build_governance_record("sec-filings-index", approved_by="human:review")
    if (approved.capability_id != SEC_FILINGS_INDEX_CAPABILITY_ID
            or any(governance[key] != expected[key] for key in (
                "expected_source_hash", "expected_schema_hash", "allowed_permissions"))):
        raise ValueError("governance does not bind the packaged SEC filings capability")
    if approved.status != "approved":
        raise ValueError("SEC filings governance is not approved")
    if active["mission_ref"] != mission["mission_ref"] or candidate["mission_ref"] != mission["mission_ref"]:
        raise ValueError("plan and active mission are not bound to the same mission_ref")
    preserved = set(active) - {"id", "created_at", "content_hash", "specs"}
    if (any(candidate[key] != active[key] for key in preserved)
            or candidate["id"] != _next_plan_id(active["id"])
            or candidate["specs"][:-1] != active["specs"]
            or len(candidate["specs"]) != len(active["specs"]) + 1
            or candidate["specs"][-1]["form"] != "8-K"):
        raise ValueError("candidate changes more than the reviewed 8-K addition")
    covered = {member["company_ref"] for member in mission["universe"]}
    if not set(candidate["companies"]).issubset(covered):
        raise ValueError("candidate contains companies outside the active mission")
    version = candidate["id"].rsplit(":", 1)[-1]
    base = {
        "schema_version": "sec-discovery-proposal-0.1",
        "created_at": created_at,
        "status": "proposed",
        "decision_required": "owner_publish_sec_8k_discovery_plan",
        "prior_plan": {"ref": active["id"], "hash": active["content_hash"]},
        "candidate_plan": {"ref": candidate["id"], "hash": candidate["content_hash"]},
        "mission_binding": {"ref": mission["id"], "hash": mission["content_hash"]},
        "governance_binding": {"ref": governance["id"], "hash": governance["content_hash"]},
        "governance_change": None,
        "publication": {
            "source_path": f"deploy/phase10/p10-us-it-services-sec-filings-plan-v{version}.candidate.json",
            "target_path": f"discovery-plans/us-it-services-sec-filings-v{version}.json",
        },
    }
    return {**base, "content_hash": content_hash(base)}


def build_selector_proposal(candidate_plan: Mapping[str, Any]) -> dict[str, Any]:
    """Produce the exact dormant selector the owner may approve in place."""

    candidate = validate_discovery_plan(candidate_plan)
    if candidate["source_ref"] != SEC_SOURCE_REF:
        raise ValueError("candidate plan is not for SEC filings")
    version = candidate["id"].rsplit(":", 1)[-1]
    body = {
        "schema_version": "sec-discovery-plan-selection-0.1",
        "id": f"sec-discovery-plan-selection:us-it-services:{version}",
        "status": "proposed",
        "source_ref": SEC_SOURCE_REF,
        "plan_ref": candidate["id"],
        "plan_hash": candidate["content_hash"],
        "plan_path": f"us-it-services-sec-filings-v{version}.json",
    }
    return {**body, "content_hash": content_hash(body)}


def verify_mission_authority_on_backup(source_core: Path, mission: Mapping[str, Any]) -> None:
    """Exercise the real authorization path on a temporary SQLite backup."""

    with tempfile.TemporaryDirectory() as tmp:
        source = sqlite3.connect(f"file:{source_core}?mode=ro", uri=True)
        target = sqlite3.connect(str(Path(tmp) / "core.sqlite"))
        try:
            source.backup(target)
        finally:
            source.close()
            target.close()
        store = DaltonStore(str(Path(tmp) / "core.sqlite"))
        try:
            authority = CoverageMissionAuthority(store)
            for company in mission["universe"]:
                authorization = authority.authorize_source_discovery(
                    company_ref=company["company_ref"], source_ref=SEC_SOURCE_REF,
                    requested_by=mission["autonomy"]["automation_principal"],
                    mission_version_ref=mission["id"],
                    mission_version_hash=mission["content_hash"],
                )
                if authorization["mission_version_hash"] != mission["content_hash"]:
                    raise RuntimeError("mission authority returned a different binding")
        finally:
            store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--active-plan", type=Path, required=True)
    parser.add_argument("--source-core", type=Path, required=True)
    parser.add_argument("--governance", type=Path, required=True)
    parser.add_argument("--created-at", default=datetime.now(timezone.utc).isoformat(timespec="microseconds"))
    parser.add_argument("--plan-output", type=Path, required=True)
    parser.add_argument("--bundle-output", type=Path, required=True)
    parser.add_argument("--selector-output", type=Path)
    args = parser.parse_args(argv)
    selector_output = args.selector_output or args.bundle_output.with_suffix(".selector.json")
    outputs = (args.plan_output.resolve(), args.bundle_output.resolve(), selector_output.resolve())
    protected = {args.active_plan.resolve(), args.source_core.resolve(), args.governance.resolve()}
    state_root = args.source_core.resolve().parent
    if len(set(outputs)) != len(outputs) or any(
            path in protected or path.is_relative_to(state_root) for path in outputs):
        raise ValueError("proposal outputs must be distinct and outside the source state directory")
    active = load_discovery_plan(args.active_plan)
    candidate = build_candidate_plan(active, created_at=args.created_at)
    mission = read_active_mission(args.source_core, active["mission_ref"])
    verify_mission_authority_on_backup(args.source_core, mission)
    governance = json.loads(args.governance.read_text(encoding="utf-8"))
    bundle = build_review_bundle(
        active_plan=active, candidate_plan=candidate, mission=mission,
        governance=governance, created_at=args.created_at,
    )
    selector = build_selector_proposal(candidate)
    for path, value in ((args.plan_output, candidate), (args.bundle_output, bundle),
                        (selector_output, selector)):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        path.chmod(0o600)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
