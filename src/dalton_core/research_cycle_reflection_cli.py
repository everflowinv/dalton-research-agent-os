"""Q2: compute one week's reflection and write it, or just show it.

    dalton-research-reflection run  --state-dir <dir>            # compute and record
    dalton-research-reflection run  --state-dir <dir> --dry-run  # compute, write nothing
    dalton-research-reflection show --state-dir <dir> --week 2026-W36

``run`` is what the lane child executes.  It opens the Core, finds the active
mission, computes the week that closed at the last Monday boundary, and writes
one row.  It makes no model call, so it has no model configuration, no
scheduler database and no budget: the whole reflection is arithmetic over rows
that are already there.

Tick summaries are read from ``--tick-summary-dir`` when one is given.  Nothing
writes that directory today -- ``run_once``'s summary goes to
``run/heartbeat.json`` and is overwritten by the next tick -- so the idle-tick
metric reports itself unavailable unless somebody has been archiving.  The
argument exists so that the day somebody does, the reflection reads it without
a code change.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from .research_cycle_reflection import (
    ResearchCycleReflectionAuthority,
    ResearchCycleReflectionError,
    build_reflection,
    closed_week,
    reflection_ref_for,
)
from .store import DaltonStore

SUMMARY_SCHEMA_VERSION = "0.1"
DEFAULT_ACTOR = "automation:coverage-mission"
# A tick archive older than this is not this week's evidence.  Bounded because
# the directory is not managed by anything: if it ever fills up, a reflection
# should get slower rather than wrong.
MAX_TICK_SUMMARIES = 4000


def _active_mission(store: DaltonStore) -> dict[str, Any]:
    from .coverage_mission import CoverageMissionAuthority

    pointer = store.connection.execute(
        "SELECT mission_version_id FROM coverage_mission_pointer ORDER BY mission_ref LIMIT 1"
    ).fetchone()
    if pointer is None:
        raise ResearchCycleReflectionError("no active mission to reflect on")
    return CoverageMissionAuthority(store).mission(pointer["mission_version_id"])


def load_tick_summaries(directory: str | Path | None) -> list[dict[str, Any]]:
    """Archived tick summaries, newest last.  Unreadable files are skipped."""

    if directory is None:
        return []
    path = Path(directory).expanduser()
    if not path.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for item in sorted(path.glob("*.json"))[-MAX_TICK_SUMMARIES:]:
        try:
            payload = json.loads(item.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(payload, dict):
            out.append(payload)
        elif isinstance(payload, list):
            out.extend(entry for entry in payload if isinstance(entry, dict))
    return out


def run_reflection(args: argparse.Namespace) -> dict[str, Any]:
    state = Path(args.state_dir).expanduser().resolve()
    store = DaltonStore(str(state / "core.sqlite"))
    try:
        mission = _active_mission(store)
        body = build_reflection(
            store.connection, mission=mission, now=datetime.now().astimezone(),
            tick_summaries=load_tick_summaries(args.tick_summary_dir),
        )
        summary: dict[str, Any] = {
            "schema_version": SUMMARY_SCHEMA_VERSION,
            "mission_ref": body["mission_ref"],
            "iso_week": body["iso_week"],
            "window": body["window"],
            "inputs_hash": body["inputs_hash"],
            "narrative": body["narrative"],
            "metrics": body["metrics"],
            "backlog_candidates": body["backlog_candidates"],
            "policy_suggestions": body["policy_suggestions"],
            "recorded": None,
        }
        if not args.dry_run:
            written = ResearchCycleReflectionAuthority(store).record(
                body, actor_ref=args.actor_ref,
            )
            summary["recorded"] = {
                "id": written["id"], "reflection_ref": written["reflection_ref"],
                "version": written["version"], "status": written["status"],
            }
        if args.summary_dir:
            target = Path(args.summary_dir).expanduser().resolve()
            target.mkdir(parents=True, exist_ok=True)
            (target / "summary.json").write_text(
                json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=1) + "\n",
                encoding="utf-8",
            )
        return summary
    finally:
        store.close()


def show_reflection(args: argparse.Namespace) -> dict[str, Any]:
    state = Path(args.state_dir).expanduser().resolve()
    store = DaltonStore(str(state / "core.sqlite"))
    try:
        mission = _active_mission(store)
        week = args.week or closed_week(datetime.now().astimezone())["iso_week"]
        authority = ResearchCycleReflectionAuthority(store)
        ref = reflection_ref_for(mission["mission_ref"], week)
        record = authority.latest(ref)
        return {
            "schema_version": SUMMARY_SCHEMA_VERSION,
            "reflection_ref": ref,
            "iso_week": week,
            "weeks": authority.weeks(mission["mission_ref"]),
            "reflection": record,
        }
    finally:
        store.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="compute and record the closed week")
    run.add_argument("--state-dir", required=True)
    run.add_argument("--tick-summary-dir",
                     help="a directory of archived controller-tick summaries")
    run.add_argument("--summary-dir", help="where the lane child leaves summary.json")
    run.add_argument("--actor-ref", default=DEFAULT_ACTOR)
    run.add_argument("--dry-run", action="store_true", help="compute but write nothing")
    run.add_argument("--quiet", action="store_true")
    run.set_defaults(handler=run_reflection)

    show = subparsers.add_parser("show", help="read one week back")
    show.add_argument("--state-dir", required=True)
    show.add_argument("--week", help="ISO week, e.g. 2026-W36; defaults to the closed week")
    show.add_argument("--quiet", action="store_true")
    show.set_defaults(handler=show_reflection)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = args.handler(args)
    except ResearchCycleReflectionError as exc:
        print(json.dumps({"status": "failed", "reason": f"{type(exc).__name__}: {exc}"},
                         ensure_ascii=False), file=sys.stderr)
        return 1
    if not args.quiet:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
