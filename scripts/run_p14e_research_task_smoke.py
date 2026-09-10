#!/usr/bin/env python3
"""P14e smoke: which of the live plan's inquiries would become research tasks.

Read-only against the live Core: the database is copied with SQLite's own
backup API and every write below lands in the copy.  No model call, no network
call, no probe.  The point is to answer, from the state the system is actually
in, three questions the report has to answer honestly:

* is ad-hoc research granted right now, and if not, which owner act is missing;
* what is today's ad-hoc pool worth against the mission's own daily cap;
* for each inquiry in the latest research plan: would it be admitted, with what
  budget, and if not, exactly why.

``--simulate-grant`` publishes the three ad-hoc ProbeTemplates *into the copy*
and reads the mission with the ``research_task`` word added in memory, so the
answer to the third question is visible before the owner has done anything.
Nothing simulated is written anywhere the live system can see.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import tempfile
from pathlib import Path

from dalton_core.bounded_planner_loop import BoundedPlannerAuthority
from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.research_task import (
    ADHOC_PROBE_TEMPLATES,
    GRANT_WORD,
    bindable_templates,
    grant,
    plan_admissions,
    pool_state,
)
from dalton_core.store import DaltonStore

SIMULATED_OWNER = "human:p14e-isolated-smoke-owner"


def run(source_core: Path, *, simulate: bool, day: str | None) -> dict:
    with tempfile.TemporaryDirectory(prefix="p14e-smoke-") as directory:
        copy = Path(directory) / "core.sqlite"
        reader = sqlite3.connect(f"{source_core.as_uri()}?mode=ro", uri=True)
        writer = sqlite3.connect(copy)
        reader.backup(writer)
        writer.close()
        reader.close()

        store = DaltonStore(str(copy))
        try:
            missions = CoverageMissionAuthority(store)
            pointer = store.connection.execute(
                "SELECT mission_version_id FROM coverage_mission_pointer "
                "ORDER BY mission_ref LIMIT 1"
            ).fetchone()
            if pointer is None:
                return {"status": "no_mission"}
            mission = missions.mission(pointer["mission_version_id"])
            authority = BoundedPlannerAuthority(store)
            live = grant(mission, bindable_templates(authority))
            report = {
                "mission_version_ref": mission["id"],
                "mission_max_daily_cost_usd": mission["budget"]["max_daily_cost_usd"],
                "live_grant": {
                    "granted": live["granted"], "reasons": live["reasons"],
                },
                "published_adhoc_templates": sorted(
                    bindable_templates(authority)
                ),
                "simulated": simulate,
            }
            if simulate:
                for spec in ADHOC_PROBE_TEMPLATES:
                    if spec.get("status") != "active":
                        # Retired in the catalogue: no executor runs it, so
                        # publishing it here would only misreport the owner's
                        # step.
                        continue
                    authority.publish_probe_template(
                        spec["template_ref"], actor_ref=SIMULATED_OWNER,
                        **{
                            field: spec[field] for field in (
                                "capability_ref", "operation", "runtime_profile_ref",
                                "parameter_contract", "output_contract_ref",
                                "verifier_ref", "permission_scope",
                                "declared_side_effects", "cost",
                            )
                        },
                    )
                mission = {
                    **mission,
                    "autonomy": {
                        **mission["autonomy"],
                        "may_write": sorted(
                            set(mission["autonomy"]["may_write"]) | {GRANT_WORD}
                        ),
                    },
                }
            templates = bindable_templates(authority)
            report["bindable_templates"] = sorted(templates)
            report["grant"] = grant(mission, templates)
            state = pool_state(authority, mission, day=day) if day else pool_state(
                authority, mission,
                day=__import__("datetime").datetime.now(
                    __import__("datetime").timezone.utc).date().isoformat(),
            )
            report["pool"] = state
            plan = missions.latest_research_plan(mission["id"])
            if plan is None:
                report["plan"] = None
                return report
            report["plan"] = {
                "plan_ref": plan["plan_id"], "created_at": plan["created_at"],
                "inquiries": len(plan["inquiries"]),
            }
            entries = plan_admissions(
                authority, mission=mission, plan=plan, templates=templates,
                day=state["day"],
            )
            report["inquiries"] = [
                {
                    "rank": entry.get("rank"),
                    "company_ref": entry.get("company_ref"),
                    "question": (entry.get("question") or "")[:96],
                    "admissible": entry["admissible"],
                    "reason": entry["reason"],
                    "inquiry_ref": entry.get("inquiry_ref"),
                    "budget": entry.get("budget"),
                    "estimated_usd": (
                        None if entry.get("estimated_micros") is None
                        else entry["estimated_micros"] / 1_000_000
                    ),
                }
                for entry in entries
            ]
            # What a widened mandate would change, without widening anything:
            # the live mandate covers the industry and Accenture only, and the
            # plan's inquiries are about the other four.
            universe = {member["company_ref"] for member in mission["universe"]}
            report["with_universe_wide_mandate"] = [
                {
                    "company_ref": entry.get("company_ref"),
                    "admissible": entry["admissible"],
                    "reason": entry["reason"],
                    "estimated_usd": (
                        None if entry.get("estimated_micros") is None
                        else entry["estimated_micros"] / 1_000_000
                    ),
                }
                for entry in plan_admissions(
                    authority, mission=mission, plan=plan, templates=templates,
                    day=state["day"],
                    scope=frozenset(universe | {mission["industry_ref"]}),
                )
            ]
            return report
        finally:
            store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--core", type=Path, required=True,
                        help="a copy of the live Core; it is copied again before use")
    parser.add_argument("--no-simulate-grant", action="store_true")
    parser.add_argument("--day", default=None)
    args = parser.parse_args(argv)
    report = run(
        args.core.expanduser().resolve(),
        simulate=not args.no_simulate_grant, day=args.day,
    )
    print(json.dumps(report, ensure_ascii=False, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
