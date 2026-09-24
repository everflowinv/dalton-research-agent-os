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

from .cockpit_model import CockpitModel, CockpitModelError, lane_status_for
from .scheduler import SchedulerError
from .coverage_mission import CoverageMissionAuthority
from .mission_stage import (
    evaluate_industry,
    evaluate_mission,
    planned_spec_refs_from_directory,
)
from .research_planner import (
    PROMPT_PROJECTION_REF,
    ResearchPlanInputTooLarge,
    ResearchPlanError,
    build_prompt,
    document_reading_priorities,
    gap_filling_inquiries,
    plan_from_response,
    project_state_for_prompt,
    prompt_size_report,
)
from .research_state import build_research_state, state_digest
from .service_config_location import service_config_path
from .store import DaltonStore, canonical_json

SUMMARY_SCHEMA_VERSION = "0.1"
# One state, one decision.  The child already replays a *stored* plan for an
# unchanged state and costs nothing; what it did not do was remember an
# attempt that produced no plan, so a state the planner had already refused --
# or one whose route was down -- re-entered every five minutes and bought a
# fresh WorkOrder each time.  Live, 2026-09-14..16: 401 rounds over 8 distinct
# states, one of them decided 317 times, 309 of the 401 ending
# ``model_unavailable``.  The ledger below is the memory that was missing.
PLAN_ATTEMPT_LEDGER = "research-plan-attempts.json"
# Bounded on purpose: this is a memory of the last few states, not a history.
MAX_LEDGER_ENTRIES = 16
# Outcomes this exact state will produce again, every time, for ever.  A
# refusal is a judgement about the state, and the state has not moved.
TERMINAL_PLAN_STATUSES: frozenset[str] = frozenset({
    "refused", "input_too_large",
})
# ...and the ones that are about this moment rather than this state: no route,
# a spent pool, a lease held by another attempt.  Those are worth asking again
# -- but not every five minutes, because each one mints a WorkOrder and the
# scheduler database grew 76MB on them.
TRANSIENT_RETRY_SECONDS = 1_800
# What one planner WorkOrder may carry.  The prompt *is* the work order's
# question, and the state is inlined into it, so a 266KB prompt is a 266KB row
# in scheduler.sqlite for every tick.  The projection is therefore fitted to
# the tighter of the model's own input bound and this, and the difference
# between them is inventory that becomes a count and a hash instead of rows.
#
# Externalising the state entirely -- ``input_refs`` plus a context-pack ref,
# the shape ``human_intent.save_context_pack`` already uses -- is the real
# answer and needs ``cockpit_model.build_work``; it is named in the report.
MAX_WORK_ORDER_PROMPT_BYTES = 64_000
# The planner reads a small object and answers with a short ranked list. Both
# bounds are generous against that, and small against a filing window.
# 2026-09-16: 120,000 → 80,000. The staged projection carries the live state
# down to a measured 75KB floor (90 document identities do not compress
# further); at 262KB the plan prompt was more than three times the tokens it
# needed, which is three times the per-minute pressure on every provider
# that throttles by tokens.
MAX_INPUT_TOKENS = 80_000
MAX_OUTPUT_TOKENS = 4_000
# The router estimates a call at its *permitted* output, not its likely one:
# 13,300 tokens in and the full 4,000 out is $0.33 on this model, while a real
# plan answers in about a quarter of that. The reservation has to cover what
# the work order allows or the call is refused before it is made.
#
# P13n set this to $0.40 because $1.50 reserved for an $0.08 call pushed the
# day past the mission's $5 cap. P13am: that cap is $100 now, so the constraint
# that produced the number is gone and keeping it would only buy refusals on a
# day when the state got large. A reservation is still money the rest of the
# day cannot spend -- this is headroom, not a licence.
MAX_COST_USD = 1.50
TIMEOUT_SECONDS = 300


def _write_owner_only(path: Path, value: Any) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(canonical_json(value) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def effective_planner_model_config(
    *, state_dir: Path, model_config_path: Path,
) -> dict[str, Any]:
    """Load the route sidecar and the current explicit owner plan budget.

    Model setup writes routing, retry and day-ledger authority beside Core.
    Cockpit budget edits for purpose ``plan`` are intentionally stored in
    ``service.json#bounded_planner.config.planner_call_budget``.  The resident
    Writer already consumes that current service value, but this older
    out-of-process research-planning child used to read only the sidecar, so an
    owner input-limit change was displayed as active while this call remained
    on its packaged default.

    An absent service override preserves the child's existing sidecar/default
    behavior.  Once the owner publishes an explicit override, it wins for new
    calls and is frozen into CockpitModel's Work budget fingerprint.
    """

    path = model_config_path.expanduser().resolve()
    config = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(config, Mapping):
        raise ValueError("planner model configuration must be an object")
    result = dict(config)
    service_path = service_config_path(state_dir)
    if not service_path.is_file():
        return result
    service = json.loads(service_path.read_text(encoding="utf-8"))
    block = service.get("bounded_planner") if isinstance(service, Mapping) else None
    nested = block.get("config") if isinstance(block, Mapping) else None
    if not isinstance(nested, Mapping) or "planner_call_budget" not in nested:
        return result
    from .call_budget import default_call_budget, validate_budget_overrides

    override = validate_budget_overrides(nested["planner_call_budget"])
    effective = default_call_budget("plan", defaults={
        "max_input_tokens": MAX_INPUT_TOKENS,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "max_cost_usd": MAX_COST_USD,
        "timeout_seconds": TIMEOUT_SECONDS,
    })
    effective.update(override)
    purposes = dict(result.get("purpose_call_budgets") or {})
    purposes["plan"] = effective
    result["purpose_call_budgets"] = purposes
    return result


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
                    "SELECT COUNT(*), COALESCE(SUM(COALESCE(c.corrected_micros,s.actual_micros)),0) "
                    "FROM thesis_impact_day_settlements s LEFT JOIN "
                    "thesis_impact_settlement_corrections c ON c.admission_id=s.admission_id "
                    "WHERE substr(s.created_at,1,10)=?",
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
                budget_db: Path | None = None,
                document_inventory: dict[str, Any] | None = None) -> dict[str, Any]:
    """Assemble everything the planner is allowed to see, and nothing else."""

    planned = planned_spec_refs_from_directory(plans_dir)
    checklist = evaluate_mission(store.connection, mission, planned_specs=planned)
    industry = evaluate_industry(store.connection, mission, planned_specs=planned)
    from .metric_discovery import contested, establish_requirements, uncorroborated

    from .research_financial_state import financial_state

    financial_models: dict[str, Any] = {}
    figures: dict[str, Any] = {}
    metrics: dict[str, Any] = {}
    disputed: dict[str, Any] = {}
    acquisition: dict[str, Any] = {}
    for entry in checklist:
        company_ref = entry["company_ref"]
        financial_models[company_ref] = financial_state(store.connection, company_ref, mission)
        acquisition[company_ref] = missions.sec_dispatch_outcomes(company_ref)
        held = missions.document_figures(company_ref)
        figures[company_ref] = {
            "total": len(held),
            "by_grade": {
                grade: sum(1 for item in held if item["source_grade"] == grade)
                for grade in {item["source_grade"] for item in held}
            },
        }
        # Corroboration counts, established first and then the ones still short
        # of a second document. Passing the raw observations here put
        # "documents: 0" beside every measure and showed the planner whichever
        # ones happened to have been recorded first rather than the most cited.
        observations = missions.metric_observations(company_ref)
        metrics[company_ref] = (establish_requirements(observations)
                                + uncorroborated(observations))
        disputed[company_ref] = contested(observations)
    from .dossier_repair_feedback import read_dossier_repair_feedback
    from .document_research_inventory import load_document_inventory
    from .document_research_feedback import read_document_research_feedback

    if document_inventory is None:
        try:
            document_inventory = load_document_inventory(
                core=store, mission=mission, state_dir=Path(store.path).parent)
        except Exception as exc:  # preserve ordinary planning, disclose unavailable original reads
            document_inventory = {"status": "unavailable", "reason": type(exc).__name__}

    return build_research_state(
        mission=mission, checklist=checklist, industry=industry,
        figures_by_company=figures, financial_models_by_company=financial_models,
        metrics_by_company=metrics,
        contested_by_company=disputed, acquisition_by_company=acquisition,
        dossier_feedback_by_company=read_dossier_repair_feedback(
            Path(store.path).parent),
        readable_documents_by_company=document_inventory.get("readable_documents_by_company"),
        unavailable_documents_by_company=document_inventory.get("unavailable_documents_by_company"),
        document_research_policy=document_inventory.get("document_research_policy"),
        document_research_availability={
            key: document_inventory[key] for key in ("status", "reason", "config_hash", "unavailable_sources")
            if key in document_inventory
        },
        document_research_feedback=read_document_research_feedback(store, mission),
        budget=mission["budget"],
        spend=read_spend(store, mission, budget_db=budget_db,
                         as_of=datetime.fromisoformat(as_of)),
        as_of=as_of,
    )


def read_attempt_ledger(state_dir: Path) -> dict[str, Any]:
    """The last few states this child decided, and how each attempt ended.

    Unreadable is empty.  A corrupt or absent ledger must never stop the
    planner from planning; the worst it can cost is one repeated decision.
    """

    try:
        value = json.loads((state_dir / PLAN_ATTEMPT_LEDGER).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - absent or unreadable is "no memory"
        return {}
    return value if isinstance(value, dict) else {}


def record_attempt(state_dir: Path, state_hash: str, summary: Mapping[str, Any]) -> None:
    """Remember how this state's attempt ended, bounded and owner-only."""

    if not state_hash:
        return
    ledger = read_attempt_ledger(state_dir)
    previous = ledger.get(state_hash) or {}
    ledger[state_hash] = {
        "at": summary.get("created_at"),
        "status": summary.get("status"),
        "plan_status": summary.get("plan_status"),
        "attempts": int(previous.get("attempts") or 0) + 1,
        "reason": (str(summary.get("failure_reason"))[:300]
                   if summary.get("failure_reason") else None),
        # A prompt that did not fit is a verdict of this projection rule, not
        # of the state alone; a new rule may fit the same state.
        "projection_rule_ref": PROMPT_PROJECTION_REF,
    }
    ordered = sorted(ledger.items(), key=lambda item: str(item[1].get("at") or ""),
                     reverse=True)[:MAX_LEDGER_ENTRIES]
    try:
        _write_owner_only(state_dir / PLAN_ATTEMPT_LEDGER, dict(ordered))
    except Exception:  # noqa: BLE001 - a ledger we cannot write is still no crash
        return


def attempt_hold(
    ledger: Mapping[str, Any], state_hash: str, *, now: datetime,
) -> dict[str, Any] | None:
    """Whether this exact state was already decided, and may not be re-asked.

    Asked **before** the model is built, so a hold costs no WorkOrder, no
    routing decision and no row in the scheduler.  That is the whole point: the
    child used to hold only on a *stored* plan, which meant every outcome that
    stored nothing -- a refusal, a dead route, a spent pool -- re-entered on
    the next tick with the same state and minted another WorkOrder.
    """

    record = ledger.get(state_hash)
    if not isinstance(record, Mapping) or not record.get("at"):
        return None
    plan_status = str(record.get("plan_status") or "")
    reason = record.get("reason")
    if (plan_status == "input_too_large"
            and record.get("projection_rule_ref") != PROMPT_PROJECTION_REF):
        # Decided under an older projection rule (or before the ledger named
        # one).  The same state may fit now, so it is asked once more.
        return None
    if plan_status in TERMINAL_PLAN_STATUSES:
        return {
            "plan_status": "held",
            "failure_reason": (
                "研究状态没有变化，没有必要重新决策：上一轮用同一份状态问过，"
                f"结果是 {plan_status}"
                + (f"（{reason}）" if reason else "")
                + "；同一个状态只会得到同一个拒绝，所以这一轮不建 work order"),
            "held_reason": "terminal_for_this_state",
            "held_attempts": int(record.get("attempts") or 0),
        }
    try:
        since = (now - datetime.fromisoformat(str(record["at"]))).total_seconds()
    except (TypeError, ValueError):
        return None
    if since < TRANSIENT_RETRY_SECONDS:
        return {
            "plan_status": "held",
            "failure_reason": (
                "研究状态没有变化，没有必要重新决策：上一轮用同一份状态问过，"
                f"{int(since)} 秒前以 {plan_status or record.get('status')} 结束"
                + (f"（{reason}）" if reason else "")
                + f"；{TRANSIENT_RETRY_SECONDS} 秒之内不再重复建 work order"),
            "held_reason": "transient_backoff",
            "held_attempts": int(record.get("attempts") or 0),
        }
    return None


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
        "sufficiency": 0,
        "replayed": False,
        "cost_micros": 0,
        "failure_reason": None,
        # 0 by design, not by accident: a plan is a proposal about the
        # system's own work and never a Claim about the world.  Kept in the
        # summary so a parent reading every lane's summary the same way still
        # gets an answer; ``plan_status`` and the two counts below are what
        # say whether this lane landed anything.
        "formal_authority_writes": 0,
        "prompt_input": None,
        # What a fresh plan actually asks the reading lanes to do next, and
        # how much of it anybody can execute today.
        "reading_priorities": [],
        "addressable_inquiries": 0,
        "held_reason": None,
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
                "sufficiency": len(existing.get("sufficiency") or []),
                "plan_ref": existing["plan_id"],
                "reading_priorities": document_reading_priorities(existing),
                "addressable_inquiries": sum(
                    1 for row in gap_filling_inquiries(existing) if row["addressable"]),
            })
            return summary
        # The same state that produced no plan last time will produce no plan
        # this time. Held here, before the model exists, so nothing is enqueued.
        held = attempt_hold(read_attempt_ledger(state_dir), state["content_hash"], now=now)
        if held is not None and not dry_run:
            summary.update({"status": "held", **held})
            return summary
        if dry_run or model_config_path is None:
            prompt_report = prompt_size_report(state)
            summary["prompt_input"] = prompt_report
            summary.update({"status": "succeeded", "plan_status": "gated",
                            "failure_reason": None if dry_run else "no planner model configured",
                            "prompt_bytes": prompt_report["prompt_bytes"]})
            return summary
        model_config = effective_planner_model_config(
            state_dir=state_dir, model_config_path=model_config_path)
        model = CockpitModel(
            model_config,
            scheduler_db=str(scheduler_db or (state_dir / "scheduler.sqlite")),
            max_input_tokens=MAX_INPUT_TOKENS, max_output_tokens=MAX_OUTPUT_TOKENS,
            max_cost_usd=MAX_COST_USD, timeout_seconds=TIMEOUT_SECONDS,
        )
        call_budget = model.budget_for("plan")
        # The tighter of what the model accepts and what one work order may
        # carry. The prompt is the work order's question; fitting only to the
        # model's bound is how a 266KB row lands in the scheduler every tick.
        prompt_bound = min(int(call_budget["max_input_tokens"]),
                           MAX_WORK_ORDER_PROMPT_BYTES)
        try:
            prompt_state = project_state_for_prompt(state, max_input_bytes=prompt_bound)
        except ResearchPlanInputTooLarge as exc:
            # Distinct from a route outage: no projection of *this* state fits,
            # and it will not fit on the next tick either. Named so the ledger
            # holds it instead of asking again every five minutes.
            summary.update({
                "status": "succeeded", "plan_status": "input_too_large",
                "failure_reason": f"{type(exc).__name__}: {exc}",
                "prompt_bytes": exc.report["prompt_bytes"],
                "prompt_input": exc.report,
            })
            return summary
        prompt = build_prompt(prompt_state)
        summary["prompt_bytes"] = len(prompt.encode("utf-8"))
        summary["work_order_prompt_bound"] = MAX_WORK_ORDER_PROMPT_BYTES
        summary["prompt_input"] = {
            **prompt_size_report(prompt_state, max_input_bytes=prompt_bound),
            "full_state_prompt_bytes": len(build_prompt(state).encode("utf-8")),
            "projection": prompt_state.get("prompt_projection"),
        }
        try:
            call = model.call(
                purpose="plan",
                # Keyed by the state, so the same world replays instead of
                # being paid for again.
                request_id=state["content_hash"][:32],
                prompt=prompt, mission=mission,
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
            summary.update({
                "status": "succeeded",
                # C2: a spent pool is a budget decision, not an outage.
                "plan_status": lane_status_for(exc, "model_unavailable"),
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
            "sufficiency": len(plan.get("sufficiency") or []),
            "assessment": plan["assessment"], "plan_ref": stored["plan_id"],
            "reading_priorities": document_reading_priorities(plan),
            "addressable_inquiries": sum(
                1 for row in gap_filling_inquiries(plan) if row["addressable"]),
        })
        return summary
    except Exception as exc:  # unexpected: record for the parent, then surface
        summary["failure_reason"] = f"unexpected {type(exc).__name__}: {exc}"
        raise
    finally:
        # Remembered whatever happened, including the crash path: an attempt
        # that ended in an unexpected exception is still an attempt against
        # this state, and repeating it every five minutes is what filled the
        # scheduler.
        if summary.get("state_hash"):
            record_attempt(state_dir, str(summary["state_hash"]), summary)
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
    # ``held`` is a decision, not a failure: the state has not moved, so
    # there was nothing to decide. Exiting non-zero would make the parent
    # read a correct, cheap tick as a broken child.
    return 0 if summary["status"] in ("succeeded", "idle", "held") else 1


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    sys.exit(main())


__all__ = ["MAX_COST_USD", "build_state", "main", "run_planner"]
