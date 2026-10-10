"""Move an existing weekly schedule to the cockpit's file authority.

Run during a stopped-service upgrade. Planning is read-only; applying never
creates a schedule or sends a brief. An existing schedule keeps its exact hash.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .store import content_hash
from .weekly_brief_coordinator import WeeklyBriefCoordinatorConfig
from .weekly_brief_workspace_plan import (
    WorkspaceWeeklyBriefConflict, _atomic_write, _write_plan, plan_path, read_plan,
)


def migrate(state_dir: Path, service_config: Path, *, apply: bool = False) -> dict[str, Any]:
    state = state_dir.resolve()
    original = json.loads(service_config.read_text(encoding="utf-8"))
    updated = json.loads(json.dumps(original))
    weekly = original.get("weekly_brief") or {}
    config = weekly.get("config") or {}
    held = read_plan(state)
    inline = None
    if config:
        parsed = WeeklyBriefCoordinatorConfig.from_mapping(config)
        if parsed.writer_socket.resolve() != state / "run/writer.sock" or parsed.token_config.resolve() != state / "writer-tokens.json":
            raise ValueError("weekly brief config belongs to another state directory")
        if parsed.plan_path is not None and parsed.plan_path.resolve() != plan_path(state):
            raise ValueError("weekly brief plan uses a different file; reconcile explicitly")
        inline = parsed.plan.to_dict() if parsed.plan is not None else None
    if inline is not None and held is not None and content_hash(inline) != content_hash(held):
        raise WorkspaceWeeklyBriefConflict("inline and cockpit weekly plans differ; reconcile explicitly")
    # A disabled lane can contain a deliberately suspended schedule. Preserve
    # that decision rather than silently re-enabling delivery during upgrade.
    if weekly.get("enabled") is not True and (inline is not None or held is not None):
        raise ValueError("a disabled lane has a schedule; explicit resumption is required")
    updated["weekly_brief"] = {
        "enabled": True,
        "interval_seconds": weekly.get("interval_seconds", 300),
        "config": {"writer_socket": str(state / "run/writer.sock"),
                   "token_config": str(state / "writer-tokens.json"),
                   "plan_path": str(plan_path(state))},
    }
    from .service import ServiceConfig
    ServiceConfig.from_mapping(updated)
    changed = updated != original or (inline is not None and held is None)
    plan = inline or held
    receipt = {"changed": changed, "applied": apply and changed,
               "before_config_hash": content_hash(original),
               "after_config_hash": content_hash(updated),
               "plan_hash": content_hash(plan) if plan else None,
               "schedule_preserved": plan is not None,
               "scheduled": plan is not None}
    if apply and changed:
        # Recheck before any mutation, and retain the old config by content hash.
        if json.loads(service_config.read_text(encoding="utf-8")) != original:
            raise WorkspaceWeeklyBriefConflict("service config changed during migration")
        backup = state / "weekly-brief-config-migrations" / (content_hash(original) + ".json")
        if not backup.exists():
            _atomic_write(backup, original)
        if inline is not None and held is None:
            _write_plan(state, inline)
        _atomic_write(service_config, updated)
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--service-config", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(migrate(args.state_dir, args.service_config, apply=args.apply), indent=2))


if __name__ == "__main__":
    main()
