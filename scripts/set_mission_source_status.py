#!/usr/bin/env python3
"""Publish the next mission version with one source's status changed, and nothing else.

The mission's ``source_plan`` is a list of ``{source_ref, role, status}`` rows
and ``status`` is a fact the mission document states about the world:
``connected``, ``probe_only`` or ``not_connected``.  The needs-human list reads
it literally -- a source the plan calls ``not_connected`` is "一类问题永远答不了"
until somebody either connects it or takes it out of the plan.  This script is
the honest way to change that sentence: every other field of the mission is
copied byte-for-byte into version N+1, and the publish goes through the live
writer's ephemeral human principal like every other mission change.

    # read-only: show the current row and the version that would be published
    .venv/bin/python scripts/set_mission_source_status.py \
        --state-dir "~/Library/Application Support/Dalton/state/dalton-core" \
        --source source:company-ir --status connected

    # publish for real
    ... --apply --actor human:lumos

``--remove`` drops the row instead of changing its status (for a source the
research does not actually need).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dalton_core.coverage_mission import SOURCE_STATUSES  # noqa: E402

BODY_FIELDS = (
    "title", "objective", "industry_ref", "universe", "research_questions",
    "deliverables", "source_plan", "bindings", "autonomy", "budget",
)


def current_mission(core_db: Path) -> dict[str, Any]:
    with sqlite3.connect(f"file:{core_db}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT v.record_json FROM coverage_mission_versions v "
            "JOIN coverage_mission_pointer p ON p.mission_version_id=v.mission_version_id "
            "ORDER BY p.updated_at DESC LIMIT 1").fetchone()
    if row is None:
        raise SystemExit("no active mission in this Core")
    return json.loads(row["record_json"])


def next_version_params(mission: dict[str, Any], *, source_ref: str,
                        status: str | None, remove: bool, request_id: str) -> dict[str, Any]:
    plan = [dict(row) for row in mission["source_plan"]]
    matches = [row for row in plan if row.get("source_ref") == source_ref]
    if len(matches) != 1:
        raise SystemExit(f"expected one source_plan row for {source_ref}, found {len(matches)}")
    if remove:
        plan = [row for row in plan if row.get("source_ref") != source_ref]
    else:
        if matches[0]["status"] == status:
            raise SystemExit(f"{source_ref} is already {status}; nothing to publish")
        matches[0]["status"] = status
    version = int(mission["version"]) + 1
    slug = mission["mission_ref"].split(":", 1)[1]
    params = {
        "mission_ref": mission["mission_ref"],
        **{field: json.loads(json.dumps(mission[field])) for field in BODY_FIELDS},
        "source_plan": plan,
        "version_id": f"coverage-mission-version:{slug}:{version}",
        "prior_version_ref": mission["id"],
        "idempotency_key": f"{mission['mission_ref']}:{version}:source-status:{request_id}",
    }
    return params


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--source", required=True, help="e.g. source:company-ir")
    parser.add_argument("--status", choices=sorted(SOURCE_STATUSES))
    parser.add_argument("--remove", action="store_true")
    parser.add_argument("--actor", default=None, help="human:<owner>; required with --apply")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if bool(args.status) == bool(args.remove):
        parser.error("choose exactly one of --status or --remove")
    state = Path(args.state_dir).expanduser()
    mission = current_mission(state / "core.sqlite")
    import uuid
    params = next_version_params(mission, source_ref=args.source, status=args.status,
                                 remove=args.remove, request_id=uuid.uuid4().hex)
    print(json.dumps({
        "current_version": mission["id"], "next_version": params["version_id"],
        "source_plan_after": params["source_plan"],
    }, ensure_ascii=False, indent=1))
    if not args.apply:
        print("dry run; add --apply --actor human:<owner> to publish", file=sys.stderr)
        return 0
    if not args.actor or not args.actor.startswith("human:"):
        parser.error("--apply needs --actor human:<owner>")
    from dalton_core.governance_cli import ephemeral_call
    result = ephemeral_call(
        state / "writer-tokens.json", state / "run" / "writer.sock",
        actor_ref=args.actor, operation="create_coverage_mission", params=params)
    print(json.dumps(result if isinstance(result, (dict, list)) else {"result": result},
                     ensure_ascii=False, indent=1)[:1500])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
