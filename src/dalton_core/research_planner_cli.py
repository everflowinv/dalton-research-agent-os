"""P13n: the child that decides what the research works on next.

Out of process, like every other lane that calls a model: a planner call is
budgeted at 300 seconds and the writer abandons a request after 30, so running
it inside the writer would report the whole lane dark exactly the way the
discovery lane was (P12e).

One run does four things and stops:

1. assemble the research state -- the goal, every company's checklist with what
   is held and what is missing and why, the industry's own checklist, the
   figures collected, the measures the market has been seen citing, the budget
   and the spend;
2. if a plan already exists for that exact state, return it and pay nothing.
   An unchanged world does not need deciding twice, and this is the whole
   reason the state hashes;
3. otherwise ask the planner model, and verify the answer against the state it
   was given -- a plan naming work that does not exist is refused whole;
4. store it.

Exit 0 when the run completed, including when it decided nothing needed doing.
``formal_authority_writes`` is always 0: a plan is a proposal about the
system's own work, never a Claim about the world.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .cockpit_model import CockpitModel, CockpitModelError
from .scheduler import SchedulerError
from .coverage_mission import CoverageMissionAuthority
from .mission_stage import (
    evaluate_industry,
    evaluate_mission,
    planned_spec_refs_from_directory,
)
from .research_planner import (
    ResearchPlanError,
    build_prompt,
    plan_from_response,
)
from .research_state import build_research_state, state_digest
from .store import DaltonStore, canonical_json

SUMMARY_SCHEMA_VERSION = "0.1"
# The planner reads a small object and answers with a short ranked list. Both
# bounds are generous against that, and small against a filing window.
MAX_INPUT_TOKENS = 120_000
MAX_OUTPUT_TOKENS = 4_000
# P13n: sized to the call, not to a comfortable round number. The state is
# ~3,300 tokens in and the answer ~1,000 out, which is about $0.08 on the
# planner's model; reserving $1.50 for it pushed the day past the mission's $5
# cap and the call was refused before it was made. A reservation is money the
# rest of the day cannot spend.
# The router estimates a call at its *permitted* output, not its likely one:
# 13,300 tokens in and the full 4,000 out is $0.33 on this model, while a real
# plan answers in about a quarter of that. The reservation has to cover what
# the work order allows or the call is refused before it is made -- and it has
# to be sized to the call rather than to a round number, because $1.50 reserved
# for an $0.08 call pushed the day past the mission's $5 cap.
MAX_COST_USD = 0.40
TIMEOUT_SECONDS = 300


def _write_owner_only(path: Path, value: Any) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(canonical_json(value) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def read_spend(store: DaltonStore, mission: Mapping[str, Any], *,
               budget_db: Path | None, as_of: datetime) -> dict[str, Any]:
    """What has actually been spent, per lane, against what it is allowed.

    P13u: the first Astra plan said so itself -- "spend is not reported, so
    remaining capacity cannot be calculated" -- because this was passed empty.
    A planner that cannot see what is left will rank work it cannot afford,
    and cannot tell "stop, that is exhausted" from "stop, that is finished".

    Every number is read, never estimated. A lane whose ledger cannot be read
    is reported absent rather than as zero: zero spend and unknown spend lead
    to opposite decisions.
    """

    from .bounded_alphaengine_probe import count_recent_alphaengine_calls
    from .public_web_core_fetch import count_recent_public_web_fetch_calls
    from .public_web_core_search import count_recent_web_search_calls

    spend: dict[str, Any] = {}
    budget = mission.get("budget") or {}
    try:
        calls = count_recent_alphaengine_calls(store.connection, as_of=as_of)
        cap = int(budget.get("max_alphaengine_calls_24h") or 0)
        spend["alphaengine_24h"] = {
            "spent": calls, "cap": cap, "remaining": max(0, cap - calls),
        }
    except Exception:  # noqa: BLE001 - unreadable is not zero
        pass
    try:
        spend["web_search_24h"] = {
            "searches": count_recent_web_search_calls(store.connection, as_of=as_of),
            "fetches": count_recent_public_web_fetch_calls(store.connection, as_of=as_of),
        }
    except Exception:  # noqa: BLE001
        pass
    if budget_db is not None and Path(budget_db).is_file():
        try:
            import sqlite3

            day = as_of.date().isoformat()
            read = sqlite3.connect(f"file:{Path(budget_db)}?mode=ro", uri=True)
            try:
                row = read.execute(
                    "SELECT COUNT(*), COALESCE(SUM(actual_micros),0) "
                    "FROM thesis_impact_day_settlements WHERE substr(created_at,1,10)=?",
                    (day,),
                ).fetchone()
            finally:
                read.close()
            cap_usd = float(budget.get("max_daily_cost_usd") or 0)
            spent_usd = round(int(row[1]) / 1_000_000, 4)
            spend["model_today"] = {
                "calls": int(row[0]),
                "cost_usd": spent_usd,
                "cost_cap_usd": cap_usd,
                "remaining_usd": round(max(0.0, cap_usd - spent_usd), 4),
                "call_cap": int(budget.get("max_daily_paid_calls") or 0),
            }
        except Exception:  # noqa: BLE001
            pass
    return spend


def build_state(store: DaltonStore, missions: CoverageMissionAuthority,
                mission: dict[str, Any], *, plans_dir: Path, as_of: str,
                budget_db: Path | None = None) -> dict[str, Any]:
    """Assemble everything the planner is allowed to see, and nothing else."""

    planned = planned_spec_refs_from_directory(plans_dir)
    checklist = evaluate_mission(store.connection, mission, planned_specs=planned)
    industry = evaluate_industry(store.connection, mission, planned_specs=planned)
    figures: dict[str, Any] = {}
    metrics: dict[str, Any] = {}
    for entry in checklist:
        company_ref = entry["company_ref"]
        held = missions.document_figures(company_ref)
        figures[company_ref] = {
            "total": len(held),
            "by_grade": {
                grade: sum(1 for item in held if item["source_grade"] == grade)
                for grade in {item["source_grade"] for item in held}
            },
        }
        metrics[company_ref] = missions.metric_observations(company_ref)
    return build_research_state(
        mission=mission, checklist=checklist, industry=industry,
        figures_by_company=figures, metrics_by_company=metrics,
        budget=mission["budget"],
        spend=read_spend(store, mission, budget_db=budget_db,
                         as_of=datetime.fromisoformat(as_of)),
        as_of=as_of,
    )


def run_planner(
    *,
    state_dir: Path,
    model_config_path: Path | None,
    summary_dir: Path,
    scheduler_db: Path | None,
    plans_dir: Path | None,
    dry_run: bool = False,
) -> dict[str, Any]:
    state_dir = state_dir.expanduser().resolve()
    summary_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    summary: dict[str, Any] = {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "created_at": now.isoformat(timespec="microseconds"),
        "status": "failed",
        "mode": "dry_run" if dry_run else "model",
        "state_hash": None,
        "plan_status": None,
        "directives": 0,
        "inquiries": 0,
        "replayed": False,
        "cost_micros": 0,
        "failure_reason": None,
        "formal_authority_writes": 0,
    }
    store = DaltonStore(str(state_dir / "core.sqlite"))
    try:
        missions = CoverageMissionAuthority(store)
        pointer = store.connection.execute(
            "SELECT mission_version_id FROM coverage_mission_pointer ORDER BY mission_ref LIMIT 1"
        ).fetchone()
        if pointer is None:
            summary.update({"status": "idle", "plan_status": "no_mission"})
            return summary
        mission = missions.mission(pointer["mission_version_id"])
        state = build_state(
            store, missions, mission,
            plans_dir=plans_dir or (state_dir / "discovery-plans"),
            as_of=now.isoformat(timespec="microseconds"),
            budget_db=state_dir / "thesis-impact-budget.sqlite",
        )
        summary["state_hash"] = state["content_hash"]
        summary["digest"] = state_digest(state)
        # An unchanged world does not need deciding twice. This is the whole
        # reason the state hashes, and it is what keeps an expensive model
        # affordable on a five-minute tick.
        existing = missions.research_plan_for_state(mission["id"], state["content_hash"])
        if existing is not None:
            summary.update({
                "status": "succeeded", "plan_status": "unchanged", "replayed": True,
                "directives": len(existing["directives"]),
                "inquiries": len(existing["inquiries"]),
                "plan_ref": existing["plan_id"],
            })
            return summary
        if dry_run or model_config_path is None:
            summary.update({"status": "succeeded", "plan_status": "gated",
                            "failure_reason": None if dry_run else "no planner model configured",
                            "prompt_bytes": len(build_prompt(state).encode("utf-8"))})
            return summary
        model = CockpitModel(
            json.loads(Path(model_config_path).expanduser().read_text(encoding="utf-8")),
            scheduler_db=str(scheduler_db or (state_dir / "scheduler.sqlite")),
            max_input_tokens=MAX_INPUT_TOKENS, max_output_tokens=MAX_OUTPUT_TOKENS,
            max_cost_usd=MAX_COST_USD, timeout_seconds=TIMEOUT_SECONDS,
        )
        try:
            call = model.call(
                purpose="plan",
                # Keyed by the state, so the same world replays instead of
                # being paid for again.
                request_id=state["content_hash"][:32],
                prompt=build_prompt(state), mission=mission,
            )
        except SchedulerError as exc:
            # The work order is keyed by the state hash, so two runs against
            # the same unchanged world share an id -- a hand-run beside the
            # tick's child, or a stale lease from one that was killed. Another
            # attempt already holds it; that is a busy lane, not a failure, and
            # crashing here loses the summary the parent reads.
            summary.update({"status": "succeeded", "plan_status": "busy",
                            "failure_reason": f"{type(exc).__name__}: {exc}"})
            return summary
        except CockpitModelError as exc:
            summary.update({"status": "succeeded", "plan_status": "model_unavailable",
                            "failure_reason": f"{type(exc).__name__}: {exc}"})
            return summary
        summary["replayed"] = bool(call.get("replayed"))
        summary["cost_micros"] = int(call.get("cost_micros") or 0)
        try:
            plan = plan_from_response(
                state, call["text"], created_at=now.isoformat(timespec="microseconds"))
        except ResearchPlanError as exc:
            # Refused whole rather than partially applied: a ranking with the
            # invented parts removed no longer means what the model meant.
            summary.update({"status": "succeeded", "plan_status": "refused",
                            "failure_reason": f"{type(exc).__name__}: {exc}"})
            return summary
        stored = missions.record_research_plan(
            plan, decided_by=mission["autonomy"]["automation_principal"],
            work_order_ref=call.get("work_order_ref"),
        )
        summary.update({
            "status": "succeeded", "plan_status": stored["status"],
            "directives": len(plan["directives"]), "inquiries": len(plan["inquiries"]),
            "assessment": plan["assessment"], "plan_ref": stored["plan_id"],
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
    parser.add_argument("--model-config", type=Path)
    parser.add_argument("--summary-dir", type=Path, help="defaults to the state dir")
    parser.add_argument("--scheduler-db", type=Path)
    parser.add_argument("--discovery-plans", type=Path)
    parser.add_argument("--dry-run", action="store_true",
                        help="assemble the state and stop; no model call, no writes")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = run_planner(
        state_dir=args.state_dir, model_config_path=args.model_config,
        summary_dir=args.summary_dir if args.summary_dir is not None else args.state_dir,
        scheduler_db=args.scheduler_db, plans_dir=args.discovery_plans,
        dry_run=args.dry_run,
    )
    if not args.quiet:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=1))
    return 0 if summary["status"] in ("succeeded", "idle") else 1


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    sys.exit(main())


__all__ = ["MAX_COST_USD", "build_state", "main", "run_planner"]
