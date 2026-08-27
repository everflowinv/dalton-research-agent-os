#!/usr/bin/env python3
"""Run the S7f weekly brief coordinator against an isolated Core backup."""

from __future__ import annotations

import argparse
import json
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from dalton_core.agenda import AgendaStore
from dalton_core.industry_research import IndustryResearchAuthority
from dalton_core.store import DaltonStore
from dalton_core.weekly_brief import WeeklyBriefAuthority
from dalton_core.weekly_brief_coordinator import (
    WeeklyBriefSchedulePlan,
    run_weekly_brief_cycle,
)


def _object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise RuntimeError(f"{path} must contain a JSON object")
    return dict(value)


def _copy_core(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise RuntimeError(f"source Core is unavailable: {source}")
    read_only = sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)
    copy = sqlite3.connect(destination)
    try:
        read_only.backup(copy)
    finally:
        copy.close()
        read_only.close()


def _after(value: str, delta: timedelta) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise RuntimeError("plan effective_from must include timezone")
    return (parsed.astimezone(timezone.utc) + delta).isoformat(
        timespec="microseconds"
    )


def run_canary(
    source_core: Path,
    plan_path: Path,
    policy_path: Path,
    *,
    as_of: str | None = None,
) -> dict[str, Any]:
    plan_raw = _object(plan_path)
    plan = WeeklyBriefSchedulePlan.from_mapping(plan_raw)
    policy_params = _object(policy_path)
    policy = policy_params.get("policy")
    if not isinstance(policy, Mapping):
        raise RuntimeError("policy candidate is missing policy")
    weekly_rule = policy.get("weekly_brief_auto_publish")
    bindings = (
        weekly_rule.get("allowed_plan_bindings")
        if isinstance(weekly_rule, Mapping) else None
    )
    if {"plan_ref": plan.plan_ref, "plan_hash": plan.content_hash} not in (
        bindings if isinstance(bindings, list) else []
    ):
        raise RuntimeError("policy candidate does not bind the exact plan hash")
    if as_of is None:
        as_of = _after(plan.effective_from, timedelta(minutes=1))
    with tempfile.TemporaryDirectory(prefix="dalton-weekly-brief-canary-") as directory:
        copied_core = Path(directory) / "core.sqlite"
        _copy_core(source_core.resolve(), copied_core)
        with DaltonStore(copied_core) as store:
            agenda = AgendaStore(store)
            industry = IndustryResearchAuthority(store)
            weekly = WeeklyBriefAuthority(store, industry)
            active = store.active_policy()
            installed = store.create_policy(
                policy,
                policy_version_id=(
                    "policy-version:weekly-brief-canary:"
                    f"{plan.content_hash[:24]}"
                ),
                version_number=int(active["version_number"]) + 1,
                prior_version_ref=active["policy_version_id"],
                policy_ref=active["policy_ref"],
                effective_from=plan.effective_from,
                effective_until=None,
                actor_ref="human:isolated-canary-owner",
                change_reason="isolated S7f coordinator canary",
            )
            first = run_weekly_brief_cycle(
                store, weekly, agenda, plan=plan.to_dict(), as_of=as_of,
                actor_ref="core",
            )
            replay = run_weekly_brief_cycle(
                store, weekly, agenda, plan=plan.to_dict(), as_of=as_of,
                actor_ref="core",
            )
            pending = [
                item for item in agenda.pending_outbox(limit=1000)
                if item["payload"].get("cycle_ref") == first["cycle_ref"]
            ]
            integrity = weekly.integrity_report()
            ok = (
                first["status"] == "ready"
                and replay["issue_status"] == "duplicate"
                and replay["outbox_status"] == "duplicate"
                and len(pending) == 1
                and integrity["ok"]
            )
            return {
                "schema_version": "0.1",
                "canary": "weekly-brief-coordinator-isolated",
                "ok": ok, "source_core": str(source_core.resolve()),
                "source_opened_read_only": True,
                "plan_ref": plan.plan_ref, "plan_hash": plan.content_hash,
                "canary_policy_version_ref": installed["policy_version_id"],
                "as_of": as_of, "first": first, "replay": replay,
                "matching_pending_outbox_rows": len(pending),
                "weekly_brief_integrity": integrity,
                "external_delivery_attempted": False,
            }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the weekly brief coordinator on an isolated Core backup"
    )
    parser.add_argument("--source-core", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--as-of")
    args = parser.parse_args()
    result = run_canary(
        args.source_core, args.plan, args.policy, as_of=args.as_of
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
