#!/usr/bin/env python3
"""Publish the next mission version with its daily budget changed, and nothing else.

A mission's ``budget`` block is the environment's own daily caps
(``max_daily_paid_calls``, ``max_daily_cost_usd``, ``max_daily_document_reads``,
``max_alphaengine_calls_24h``, ``max_alphaengine_probe_calls_24h``).  A new
environment copies the template's numbers at its first publish; when those
numbers are wrong for the work, the honest fix is the next version of the
mission with the numbers changed and every other field byte-identical --
through the live writer's ephemeral human principal like every other change.

    # read-only: show the current block and the version that would be published
    .venv/bin/python scripts/set_mission_budget.py \\
        --state-dir /Volumes/EveSSD/Dalton/workspaces/<slug>/state/dalton-core \\
        --set max_daily_paid_calls=100000 --set max_daily_cost_usd=500

    # publish for real
    ... --apply --actor human:<owner>
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from scripts.set_mission_source_status import BODY_FIELDS, current_mission  # noqa: E402

INTEGER_KEYS = {"max_daily_paid_calls", "max_daily_document_reads",
                "max_alphaengine_calls_24h", "max_alphaengine_probe_calls_24h"}
FLOAT_KEYS = {"max_daily_cost_usd"}


def parse_setting(text: str) -> tuple[str, int | float]:
    key, _, raw = text.partition("=")
    key = key.strip()
    if key in INTEGER_KEYS:
        return key, int(raw)
    if key in FLOAT_KEYS:
        return key, float(raw)
    raise SystemExit(f"unknown budget key {key!r}; known: {sorted(INTEGER_KEYS | FLOAT_KEYS)}")


def next_version_params(mission: dict, *, changes: dict, request_id: str) -> dict:
    budget = dict(mission["budget"])
    unchanged = {k: v for k, v in changes.items() if budget.get(k) == v}
    if unchanged and len(unchanged) == len(changes):
        raise SystemExit("every requested value is already in force; nothing to publish")
    budget.update(changes)
    version = int(mission["version"]) + 1
    slug = mission["mission_ref"].split(":", 1)[1]
    return {
        "mission_ref": mission["mission_ref"],
        **{field: json.loads(json.dumps(mission[field])) for field in BODY_FIELDS},
        "budget": budget,
        "version_id": f"coverage-mission-version:{slug}:{version}",
        "prior_version_ref": mission["id"],
        "idempotency_key": f"{mission['mission_ref']}:{version}:budget:{request_id}",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--actor", default=None)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if not args.set:
        parser.error("at least one --set KEY=VALUE")
    changes = dict(parse_setting(item) for item in args.set)
    state = Path(args.state_dir).expanduser()
    mission = current_mission(state / "core.sqlite")
    params = next_version_params(mission, changes=changes, request_id=uuid.uuid4().hex)
    print(json.dumps({"current_version": mission["id"], "budget_before": mission["budget"],
                      "next_version": params["version_id"], "budget_after": params["budget"]},
                     ensure_ascii=False, indent=1))
    if not args.apply:
        print("dry run; add --apply --actor human:<owner> to publish", file=sys.stderr)
        return 0
    if not args.actor or not args.actor.startswith("human:"):
        parser.error("--apply needs --actor human:<owner>")
    # The budget is not a field a mission version may simply carry: the writer
    # refuses ``create_coverage_mission`` with a changed budget ("mission budget
    # must be published through the policy/mandate/constitution cascade").  The
    # cockpit's 预算 page uses this operation, which runs that cascade and
    # publishes the mission version last; the same door is used here.
    from dalton_core.governance_cli import ephemeral_call
    allowed = {"max_daily_paid_calls", "max_daily_cost_usd", "max_alphaengine_calls_24h",
               "max_daily_document_reads", "pools_enforcement"}
    budget = {key: value for key, value in params["budget"].items() if key in allowed}
    result = ephemeral_call(state / "writer-tokens.json", state / "run" / "writer.sock",
                            actor_ref=args.actor, operation="set_research_budget_authority_chain",
                            params={"mission_ref": mission["mission_ref"], "budget": budget,
                                    "expected_mission_hash": mission["content_hash"]})
    print(json.dumps(result if isinstance(result, (dict, list)) else {"result": result},
                     ensure_ascii=False, indent=1)[:1200])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
