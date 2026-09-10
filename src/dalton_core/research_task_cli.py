"""P14e child: admit the research plan's inquiries as bounded loops.

One run does three things and stops:

1. read the active mission, the latest research plan and the published ad-hoc
   ProbeTemplate catalogue, and decide whether ad-hoc research is granted at
   all -- the mission's ``may_write`` must carry ``research_task`` and at least
   one ad-hoc template must have an operation an executor can run;
2. work down the plan's inquiries in the planner's own order, admitting at most
   ``--max-admissions`` of them, refusing each one that is already admitted,
   outside the universe or the mandate, unbindable, or past the day's ad-hoc
   pool;
3. write a summary the lane reads back.

It makes no model call and runs no probe.  The loop it creates is advanced by
``bounded_planner_driver.run_once`` exactly like every other bounded loop --
that is the whole design: an ad-hoc research task is not a new dispatcher, it
is a loop whose question came from an inquiry.

Exit 0 when the run completed, including when it admitted nothing.
``formal_authority_writes`` is always 0: admitting a question is attention, not
a Claim about the world.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .bounded_planner_driver import BUDGET_LEDGER_FILENAME
from .bounded_planner_loop import BoundedPlannerAuthority, BoundedPlannerError
from .research_question_backlog import ResearchQuestionBacklog, ResearchQuestionError
from .research_task import (
    ResearchTaskError,
    admit_inquiry,
    bindable_templates,
    grant,
    plan_admissions,
    pool_state,
)
from .store import DaltonStore, canonical_json

SUMMARY_SCHEMA_VERSION = "0.1"
DEFAULT_MAX_ADMISSIONS = 1


def _write_owner_only(path: Path, value: Any) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(canonical_json(value) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def run_admissions(
    *,
    state_dir: Path,
    summary_dir: Path,
    max_admissions: int = DEFAULT_MAX_ADMISSIONS,
    retired_templates: tuple[str, ...] = (),
    dry_run: bool = False,
) -> dict[str, Any]:
    state_dir = state_dir.expanduser().resolve()
    summary_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    summary: dict[str, Any] = {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "created_at": now.isoformat(timespec="microseconds"),
        "status": "failed",
        "mode": "dry_run" if dry_run else "admit",
        "grant": None,
        "plan_ref": None,
        "considered": 0,
        "admitted": 0,
        "refused": [],
        "tasks": [],
        "pool": None,
        "failure_reason": None,
        "formal_authority_writes": 0,
    }
    # C2b: the same day ledger the planner's model calls are now admitted
    # against, so the pool this lane reserves from is read net of what has
    # already been spent from it today rather than of reservations alone.  A
    # state directory without the ledger reads as zero spend, which is the
    # pre-C2b number.
    budget_db = state_dir / BUDGET_LEDGER_FILENAME
    store = DaltonStore(str(state_dir / "core.sqlite"))
    try:
        from .coverage_mission import CoverageMissionAuthority

        missions = CoverageMissionAuthority(store)
        pointer = store.connection.execute(
            "SELECT mission_version_id FROM coverage_mission_pointer "
            "ORDER BY mission_ref LIMIT 1"
        ).fetchone()
        if pointer is None:
            summary.update({"status": "idle", "failure_reason": "no_mission"})
            return summary
        mission = missions.mission(pointer["mission_version_id"])
        authority = BoundedPlannerAuthority(store)
        templates = bindable_templates(authority, retired=retired_templates)
        decision = grant(mission, templates)
        summary["grant"] = decision
        day = now.date().isoformat()
        summary["pool"] = pool_state(
            authority, mission, day=day, budget_db=budget_db)
        if not decision["granted"]:
            # Not a failure: an ungranted lane is a lane the owner has not
            # turned on, and saying so every tick is how it stays visible.
            summary.update({"status": "idle", "failure_reason": "not_granted"})
            return summary
        plan = missions.latest_research_plan(mission["id"])
        if plan is None:
            summary.update({"status": "idle", "failure_reason": "no_research_plan"})
            return summary
        summary["plan_ref"] = plan["plan_id"]
        entries = plan_admissions(
            authority, mission=mission, plan=plan, templates=templates, day=day,
            # Only what this pass will actually create spends the day's pool.
            limit=max_admissions, budget_db=budget_db,
        )
        summary["considered"] = len(entries)
        summary["refused"] = [
            {
                "inquiry_ref": entry.get("inquiry_ref"),
                "company_ref": entry.get("company_ref"),
                "reason": entry["reason"],
            }
            for entry in entries if not entry["admissible"]
        ]
        if dry_run:
            summary.update({
                "status": "succeeded",
                "tasks": [
                    {
                        "inquiry_ref": entry["inquiry_ref"],
                        "loop_ref": entry["loop_ref"],
                        "subject_ref": entry["subject_ref"],
                        "budget": entry["budget"],
                        "estimated_micros": entry["estimated_micros"],
                        "status": "would_admit",
                    }
                    for entry in entries if entry["admissible"]
                ][:max_admissions],
            })
            return summary
        backlog = ResearchQuestionBacklog(store)
        admitted: list[dict[str, Any]] = []
        for entry in entries:
            if len(admitted) >= max_admissions:
                break
            if not entry["admissible"]:
                continue
            # By ordinal, not by position: the refused entries are in this list
            # too, and zipping the two lists would hand the wrong inquiry to an
            # entry the moment one is refused.
            inquiry = plan["inquiries"][entry["ordinal"]]
            try:
                admitted.append(admit_inquiry(
                    authority, backlog, mission=mission,
                    plan_ref=plan["plan_id"], inquiry=inquiry, entry=entry,
                ))
            except (
                BoundedPlannerError, ResearchQuestionError, ResearchTaskError,
            ) as exc:
                # One refused inquiry is that inquiry's, never the run's: the
                # next one may well be admissible, and a summary that names the
                # refusal is what the lane shows the owner.
                summary["refused"].append({
                    "inquiry_ref": entry["inquiry_ref"],
                    "company_ref": entry.get("company_ref"),
                    "reason": f"{type(exc).__name__}: {exc}",
                })
        summary.update({
            "status": "succeeded",
            "admitted": sum(1 for item in admitted if item["status"] == "fresh"),
            "tasks": admitted,
            "pool": pool_state(
                authority, mission, day=day, budget_db=budget_db),
        })
        return summary
    except Exception as exc:  # unexpected: record for the parent, then surface
        summary["failure_reason"] = f"unexpected {type(exc).__name__}: {exc}"
        raise
    finally:
        _write_owner_only(summary_dir / "summary.json", summary)
        store.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--summary-dir", type=Path, help="defaults to the state dir")
    parser.add_argument("--max-admissions", type=int, default=DEFAULT_MAX_ADMISSIONS)
    parser.add_argument(
        "--retired-template", action="append", default=[], dest="retired_templates",
        help="An ad-hoc ProbeTemplate this deployment has withdrawn. Repeatable.",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="decide and stop; no authority writes")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = run_admissions(
        state_dir=args.state_dir,
        summary_dir=args.summary_dir if args.summary_dir is not None else args.state_dir,
        max_admissions=max(int(args.max_admissions), 1),
        retired_templates=tuple(args.retired_templates or ()),
        dry_run=args.dry_run,
    )
    if not args.quiet:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=1))
    return 0 if summary["status"] in ("succeeded", "idle") else 1


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    sys.exit(main())


__all__ = ["DEFAULT_MAX_ADMISSIONS", "build_parser", "main", "run_admissions"]
