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
import sqlite3
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
from .research_state import (
    build_research_state,
    planning_core_hash as state_planning_core_hash,
    planning_hash as state_planning_hash,
    planning_settlement_hash as state_planning_settlement_hash,
    state_digest,
)
from .service_config_location import service_config_path
from .store import DaltonStore, canonical_json, content_hash

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
    "refused", "input_too_large", "output_recovery_failed",
})
# ...and the ones that are about this moment rather than this state: no route,
# a spent pool, a lease held by another attempt.  Those are worth asking again
# -- but not every five minutes, because each one mints a WorkOrder and the
# scheduler database grew 76MB on them.
TRANSIENT_RETRY_SECONDS = 1_800
# The last state the planner model was actually asked about (a call that
# returned, paid or replayed).  Separate from the attempt ledger, which is
# keyed by exact state and bounded to the last few: this is one record, and
# it must outlive a run of held ticks.
PLAN_LAST_DECISION = "research-plan-last-decision.json"
# The shortest gap between two paid plans when nothing material moved.  Live
# 2026-09-24 the planner asked Opus every five minutes on both workspaces
# (~$0.44 each, $100+/day) while adjacent prompts differed only in spend and
# pipeline counters.  A material change (``research_state.planning_hash``)
# still re-plans on the next tick; everything else is folded into the next
# plan at most this often.
MIN_REPLAN_SECONDS = 7_200
# 2026-10-01: the floor between the last decision and the one an owner
# withdrawal or a newly settled admission buys.  Live 2026-09-30 legacy's
# 27 withdrawals moved nothing the planner hashed, so they waited on the
# two-hour interval -- and then on a prompt that never said they were open.
# A settlement is material, but they arrive one child at a time; without a
# floor every settled read would buy a plan.  At most one plan per floor.
SETTLEMENT_REPLAN_SECONDS = 1_800
# A finite serialized Work prompt bound, in UTF-8 bytes, distinct from the
# configured model token budget. The former 64KB ceiling rejected a 64,844-byte
# identity-preserving projection even with an approved 250,000-token budget.
# Keep storage bounded (historical 266KB inline rows still do not fit); model
# Work admission and native provider controls independently enforce token,
# cost and timeout authority. A token setting is not a byte count.
# A source-bound note directory can already occupy 127KB after projection.
# Leave room for new questions while retaining the configured model input cap.
MAX_WORK_ORDER_PROMPT_BYTES = 192_000
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


def planner_input_byte_bound(*, state_dir: Path, model_config_path: Path) -> int:
    """Resolve the current conservative Work input bound without a model call.

    Only reads current configuration and the shared policy. It opens no
    scheduler/authority, and retains build_work's existing byte estimate.
    """
    from .call_budget import resolve_call_budget
    config = effective_planner_model_config(state_dir=state_dir, model_config_path=model_config_path)
    budget = resolve_call_budget(config, "plan", defaults={
        "max_input_tokens": MAX_INPUT_TOKENS, "max_output_tokens": MAX_OUTPUT_TOKENS,
        "max_cost_usd": MAX_COST_USD, "timeout_seconds": TIMEOUT_SECONDS,
    })
    return int(budget["max_input_tokens"])


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
        financial_models[company_ref] = financial_state(
            store.connection, company_ref, mission, state_dir=Path(store.path).parent)
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
    from .document_research_feedback import (
        read_document_research_feedback, read_document_research_settlement,
    )

    # 2026-10-09: what the filed statements hold (segments included) and where
    # each company's reading actually stands, with the automatic step that is
    # holding any open review.  Both read-only; neither may stop a plan.
    state_dir = Path(store.path).parent
    filed_statements: dict[str, Any] = {}
    try:
        from .statement_segments import segment_summary, statement_segment_figures

        for entry in checklist:
            filed_statements[entry["company_ref"]] = segment_summary(
                statement_segment_figures(store.connection, entry["company_ref"],
                                          state_dir=state_dir))
    except Exception as exc:  # noqa: BLE001 - unknown, never a zero
        filed_statements = {entry["company_ref"]: {
            "status": "unavailable", "reason": type(exc).__name__} for entry in checklist}
    try:
        from .research_reading_pipeline import reading_pipeline

        reading = reading_pipeline(store.connection, mission["id"], state_dir=state_dir)
    except Exception as exc:  # noqa: BLE001 - unknown, never a zero
        reading = {entry["company_ref"]: {"status": "unavailable",
                                          "reason": type(exc).__name__}
                   for entry in checklist}
    if document_inventory is None:
        try:
            document_inventory = load_document_inventory(
                core=store, mission=mission, state_dir=Path(store.path).parent)
        except Exception as exc:  # preserve ordinary planning, disclose unavailable original reads
            document_inventory = {"status": "unavailable", "reason": type(exc).__name__}

    from .initial_screen_failure_feedback import read_feedback
    try:
        failure_config = json.loads((state_dir/'initial-screen-model-config.json').read_text())
        screen_feedback = {entry['company_ref']: read_feedback(store.connection,
            state_dir=state_dir, mission=mission, company_ref=entry['company_ref'],config=failure_config)
            for entry in checklist}
    except (OSError, ValueError, KeyError, TypeError):
        screen_feedback = {}
    from .initial_screen_quality_feedback import read_feedback as read_quality_feedback
    quality_feedback = {}
    for entry in checklist:
        try:
            quality_feedback[entry['company_ref']] = read_quality_feedback(store.connection,company_ref=entry['company_ref'])
        except (ValueError, KeyError, TypeError, sqlite3.Error) as exc:
            quality_feedback[entry['company_ref']] = {'status':'unavailable','reason':type(exc).__name__}
    from .investment_memo_planner_feedback import read_feedback as read_memo_feedback
    memo_feedback = {}
    try:
        memo_config = json.loads((state_dir/'dossier-model-config.json').read_text())
        memo_feedback = {entry['company_ref']: read_memo_feedback(store.connection,
            state_dir=state_dir, mission=mission, company_ref=entry['company_ref'], config=memo_config)
            for entry in checklist}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    state = build_research_state(
        mission=mission, checklist=checklist, industry=industry,
        figures_by_company=figures, financial_models_by_company=financial_models,
        metrics_by_company=metrics,
        initial_screen_feedback_by_company=screen_feedback,
        initial_screen_quality_feedback_by_company=quality_feedback,
        memo_question_feedback_by_company=memo_feedback,
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
        document_research_settlement=read_document_research_settlement(
            store, mission, as_of=as_of),
        filed_statements_by_company=filed_statements,
        reading_pipeline_by_company=reading,
        budget=mission["budget"],
        spend=read_spend(store, mission, budget_db=budget_db,
                         as_of=datetime.fromisoformat(as_of)),
        as_of=as_of,
    )
    from .directed_numeric_research import CONTRACT_REF
    state["document_research_numeric_contract_ref"] = CONTRACT_REF

    from .canonical_document_questions import read_questions
    questions = read_questions(store.connection, mission, now=datetime.fromisoformat(as_of))
    for company in state["companies"]:
        if company["company_ref"] in questions:
            company["canonical_document_questions"] = questions[company["company_ref"]]

    from .research_followup_automation import planner_context
    from .research_state import state_content_hash
    followups = planner_context(store.connection, mission)
    if followups is not None:
        state["reflection_followup_context"] = followups
    from .research_planner import typed_note_directory_contract, TASK_HASH
    note_contract = typed_note_directory_contract(state)
    if note_contract is not None:
        # An input-contract change is one normal planning decision. A clock,
        # event or repeated tick cannot create a new permission or repair slot.
        state["planner_task_hash"] = content_hash({"task": TASK_HASH, "typed_note_directory": note_contract})
    state["content_hash"] = state_content_hash(state)
    return state


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
    if (summary.get('plan_status') == 'held' and (
            previous.get('plan_status') in {'output_recovery_failed', 'input_too_large'}
            or (summary.get('paid_output_recovery') is not None
                and previous.get('plan_status') in {'model_unavailable', 'skipped:pool_exhausted'}))):
        # Reading a size hold or terminal successor is not another attempt.
        # Preserve its reason and original bound rather than replacing it
        # with a fresh transient backoff.
        # The same applies to a proved successor waiting on a pre-send refusal:
        # later reads must retain its identity and normal retry clock.
        return
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
        "work_order_prompt_bound": MAX_WORK_ORDER_PROMPT_BYTES,
        "conservative_work_input_byte_bound": summary.get("conservative_work_input_byte_bound"),
        **({"structural_revalidation_contract": (summary.get("structural_revalidation_contract")
                 or previous.get("structural_revalidation_contract"))}
           if summary.get("structural_revalidation_contract") or previous.get("structural_revalidation_contract") else {}),
    }
    if (summary.get("plan_status") == "held" and previous.get("plan_status") in TERMINAL_PLAN_STATUSES
            and ledger[state_hash].get("structural_revalidation_contract")):
        # Free verification never turns an original terminal verdict into a
        # transient retry. Preserve its time/count/reason and remember only
        # the fixed free contract; changing model policy cannot buy it again.
        ledger[state_hash] = {**previous, "structural_revalidation_contract":
                              ledger[state_hash]["structural_revalidation_contract"]}
    ordered = sorted(ledger.items(), key=lambda item: str(item[1].get("at") or ""),
                     reverse=True)[:MAX_LEDGER_ENTRIES]
    try:
        _write_owner_only(state_dir / PLAN_ATTEMPT_LEDGER, dict(ordered))
    except Exception:  # noqa: BLE001 - a ledger we cannot write is still no crash
        return


def attempt_hold(
    ledger: Mapping[str, Any], state_hash: str, *, now: datetime,
    input_byte_bound: int | None = None,
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
            and (record.get("projection_rule_ref") != PROMPT_PROJECTION_REF
                 or record.get("work_order_prompt_bound", 64_000) != MAX_WORK_ORDER_PROMPT_BYTES
                 or (input_byte_bound is not None and
                     record.get("conservative_work_input_byte_bound") != input_byte_bound))):
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


def read_last_decision(state_dir: Path) -> dict[str, Any]:
    try:
        value = json.loads((state_dir / PLAN_LAST_DECISION).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - absent or unreadable is "no memory"
        return {}
    return value if isinstance(value, dict) else {}


def record_decision(state_dir: Path, *, state_hash: str, planning_hash: str,
                    at: str, planning_core_hash: str | None = None,
                    settlement_hash: str | None = None) -> None:
    try:
        _write_owner_only(state_dir / PLAN_LAST_DECISION, {
            "schema_version": "0.1", "at": at, "state_hash": state_hash,
            "planning_hash": planning_hash,
            "planning_core_hash": planning_core_hash,
            "settlement_hash": settlement_hash,
        })
    except Exception:  # noqa: BLE001 - a record we cannot write is still no crash
        return


def replan_pacing_hold(
    last: Mapping[str, Any], planning_hash: str, *, now: datetime,
    planning_core_hash: str | None = None,
    settlement_hash: str | None = None,
) -> dict[str, Any] | None:
    """Hold a re-plan whose state moved, but not in any way a plan is about.

    Asked before the model is built, like :func:`attempt_hold`.  A material
    change outside the Dossier feedback (a different ``planning_core_hash``
    from the last decision) is never held.  A change only in the Dossier
    feedback -- a new published version, a stable repair target joining or
    leaving -- is material but paced: live 2026-09-25 Dossier runs landed
    every five minutes and each one bought a plan.  Both it and non-material
    movement are held until ``MIN_REPLAN_SECONDS`` have passed since the last
    decision, after which the accumulated changes are worth one fresh plan.

    A decision recorded before the core hash existed has none; it is judged
    by the full ``planning_hash`` alone, as before.

    2026-10-01: a change in which directed reads are done with -- an owner
    withdrawal, or an admission reaching a terminal outcome (a different
    ``settlement_hash``) -- is material and buys one fresh plan as soon as
    ``SETTLEMENT_REPLAN_SECONDS`` have passed since the last decision, not
    after ``MIN_REPLAN_SECONDS``.  The fresh decision records the new hash,
    so the same settlement never buys a second plan.  A decision recorded
    before the settlement hash existed counts as having seen none, so the
    first tick after an upgrade folds every earlier withdrawal into one plan.
    ``settlement_hash=None`` (no readable settlement) never counts as moved.
    """

    if not last.get("at"):
        return None
    last_core = last.get("planning_core_hash")
    if planning_core_hash is not None and last_core:
        if last_core != planning_core_hash:
            return None
        feedback_only = last.get("planning_hash") != planning_hash
    else:
        if last.get("planning_hash") != planning_hash:
            return None
        feedback_only = False
    try:
        since = (now - datetime.fromisoformat(str(last["at"]))).total_seconds()
    except (TypeError, ValueError):
        return None
    if since < 0:
        return None
    if settlement_hash is not None and last.get("settlement_hash") != settlement_hash:
        if since >= SETTLEMENT_REPLAN_SECONDS:
            return None
        return {
            "plan_status": "held",
            "failure_reason": (
                "有定向研读结束了（owner 撤回，或得到了终局结果），这是研究状态的变化；"
                f"上一次规划在 {int(since)} 秒前，{SETTLEMENT_REPLAN_SECONDS} 秒后重新规划一次"),
            "held_reason": "settlement_paced",
            "held_attempts": 0,
        }
    if since >= MIN_REPLAN_SECONDS:
        return None
    if feedback_only:
        return {
            "plan_status": "held",
            "failure_reason": (
                "只有公司档案（Dossier）的反馈变了（已发布版本或稳定的修补目标）；"
                f"上一次规划在 {int(since)} 秒前，这类变化 {MIN_REPLAN_SECONDS} 秒内不重新规划"),
            "held_reason": "dossier_feedback_paced",
            "held_attempts": 0,
        }
    return {
        "plan_status": "held",
        "failure_reason": (
            "研究状态没有实质变化：与上一次规划相比，只有花费、计数这类不影响规划的字段变了；"
            f"上一次规划在 {int(since)} 秒前，{MIN_REPLAN_SECONDS} 秒内不重新规划"),
        "held_reason": "no_material_change",
        "held_attempts": 0,
    }


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
            if state.get("reflection_followup_context"):
                from .research_followup_automation import FollowupAutomationAuthority
                summary["followup_planner_observations"] = [x["id"] for x in
                    FollowupAutomationAuthority(store).observe_planner(
                        context=state["reflection_followup_context"], stored_plan=existing, full_state=state)]
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
        recovery = None
        attempts = read_attempt_ledger(state_dir)
        last_attempt = attempts.get(state['content_hash']) or {}
        from .planner_structural_revalidation import (CONTRACT as STRUCTURAL_REVALIDATION,
            candidate as revalidate_paid_plan, current_numeric_canonical_binding)
        def canonical_resolver(inquiry, complete_state):
            return current_numeric_canonical_binding(store.connection, mission, inquiry, complete_state)
        if (not dry_run and model_config_path is not None
                and last_attempt.get('plan_status') in {'refused', 'held'}
                and last_attempt.get('structural_revalidation_contract') != STRUCTURAL_REVALIDATION):
            summary['structural_revalidation_contract'] = STRUCTURAL_REVALIDATION
            # An operational ledger is only a bounded hint. Eligibility comes
            # from the exact immutable Work, complete state, receipts and fees.
            try:
                free = revalidate_paid_plan(state_dir=state_dir, scheduler_db=scheduler_db,
                    config=effective_planner_model_config(state_dir=state_dir, model_config_path=model_config_path),
                    state=state, mission=mission, validate=lambda text: plan_from_response(
                        state, text, created_at=now.isoformat(timespec='microseconds'),
                        canonical_binding_resolver=canonical_resolver))
            except (ValueError, OSError, sqlite3.Error) as exc:
                free = None
                summary['structural_revalidation'] = {'status':'unverified','reason':f'{type(exc).__name__}: {exc}'}
            if free is not None:
                stored = missions.record_research_plan(free['plan'],
                    decided_by=mission['autonomy']['automation_principal'],
                    work_order_ref=free['proof']['work_ref'])
                record_decision(state_dir,state_hash=state['content_hash'],
                    planning_hash=state_planning_hash(state), planning_core_hash=state_planning_core_hash(state),
                    settlement_hash=state_planning_settlement_hash(state),at=now.isoformat(timespec='microseconds'))
                if state.get('reflection_followup_context'):
                    from .research_followup_automation import FollowupAutomationAuthority
                    summary['followup_planner_observations'] = [x['id'] for x in
                        FollowupAutomationAuthority(store).observe_planner(
                            context=state['reflection_followup_context'], stored_plan=stored, full_state=state)]
                summary.update({'status':'succeeded','plan_status':stored['status'],'replayed':True,
                    'plan_ref':stored['plan_id'],'directives':len(free['plan']['directives']),
                    'inquiries':len(free['plan']['inquiries']),'sufficiency':len(free['plan'].get('sufficiency') or []),
                    'assessment':free['plan']['assessment'], 'structural_revalidation':free['proof'],
                    'reading_priorities':document_reading_priorities(free['plan']),
                    'addressable_inquiries':sum(1 for row in gap_filling_inquiries(free['plan']) if row['addressable'])})
                return summary
        if (not dry_run and model_config_path is not None
                and last_attempt.get('plan_status') in {'model_unavailable', 'skipped:pool_exhausted'}):
            from .planner_paid_output_recovery import planner_candidate
            recovery = planner_candidate(state_dir=state_dir, config_path=model_config_path,
                mission=mission, state_hash=state['content_hash'], now=now,
                scheduler_db=scheduler_db, include_payload=True)
        if recovery is not None:
            summary['paid_output_recovery'] = {k:v for k,v in recovery.items() if k!='_parent_prompt'}
        current_input_bound = None
        if not dry_run and model_config_path is not None and last_attempt.get("plan_status") == "input_too_large":
            current_input_bound = planner_input_byte_bound(
                state_dir=state_dir, model_config_path=model_config_path)
        held = attempt_hold(attempts, state["content_hash"], now=now,
                            input_byte_bound=current_input_bound)
        retry_ready = recovery is not None and recovery['successor_status'] != 'failed'
        if held is not None and not retry_ready and not dry_run:
            summary.update({"status": "held", **held})
            return summary
        summary["planning_hash"] = state_planning_hash(state)
        summary["planning_core_hash"] = state_planning_core_hash(state)
        summary["settlement_hash"] = state_planning_settlement_hash(state)
        paced = replan_pacing_hold(
            read_last_decision(state_dir), summary["planning_hash"], now=now,
            planning_core_hash=summary["planning_core_hash"],
            settlement_hash=summary["settlement_hash"])
        if paced is not None and not retry_ready and not dry_run:
            summary.update({"status": "held", **paced})
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
        # Storage is independently finite. Existing build_work admission also
        # uses UTF-8 bytes as a conservative upper estimate against the input
        # token authority. Respect that preflight when filling optional context;
        # this estimate is not a provider token count and does not raise its cap.
        conservative_work_input_bytes = int(call_budget["max_input_tokens"])
        prompt_bound = min(MAX_WORK_ORDER_PROMPT_BYTES, conservative_work_input_bytes)
        # Persist the actual bound even when projection fails before any Work.
        summary["conservative_work_input_byte_bound"] = conservative_work_input_bytes
        summary["work_order_prompt_bound"] = MAX_WORK_ORDER_PROMPT_BYTES
        summary["model_max_input_tokens"] = conservative_work_input_bytes
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
        if recovery is not None:
            # Same full state, exact original prompt and one deterministic
            # successor. A new read timestamp does not open another generation.
            prompt = recovery['_parent_prompt']
        summary["prompt_bytes"] = len(prompt.encode("utf-8"))
        summary["work_order_prompt_bound"] = MAX_WORK_ORDER_PROMPT_BYTES
        summary["model_max_input_tokens"] = int(call_budget["max_input_tokens"])
        summary["conservative_work_input_byte_bound"] = conservative_work_input_bytes
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
                request_id=(state["content_hash"][:32] if recovery is None
                            else recovery['parent_request_id']+recovery['suffix']),
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
            from .cockpit_model import model_failure_trace
            summary['model_failure_trace'] = model_failure_trace(exc)
            summary.update({
                "status": "succeeded",
                # C2: a spent pool is a budget decision, not an outage.
                "plan_status": lane_status_for(exc, "model_unavailable"),
                "failure_reason": f"{type(exc).__name__}: {exc}"})
            trace = summary['model_failure_trace']
            if (recovery is not None and trace is not None
                    and ':paid-output-recovery:' in trace['work_request_id']):
                from .planner_paid_output_recovery import proved_paid_successor
                if proved_paid_successor(scheduler_db=model.scheduler_db,
                        config=model.config,mission=mission,recovery=recovery,
                        now=datetime.now(timezone.utc)):
                    # Only another full-settled output overrun is terminal;
                    # a pre-send pool/route refusal keeps ordinary reentry.
                    summary['plan_status'] = 'output_recovery_failed'
            return summary
        summary["replayed"] = bool(call.get("replayed"))
        summary["cost_micros"] = int(call.get("cost_micros") or 0)
        record_decision(state_dir, state_hash=state["content_hash"],
                        planning_hash=summary["planning_hash"],
                        planning_core_hash=summary["planning_core_hash"],
                        settlement_hash=summary["settlement_hash"],
                        at=now.isoformat(timespec="microseconds"))
        try:
            plan = plan_from_response(
                state, call["text"], created_at=now.isoformat(timespec="microseconds"),
                canonical_binding_resolver=canonical_resolver)
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
        from .research_followup_automation import FollowupAutomationAuthority
        if state.get("reflection_followup_context"):
            summary["followup_planner_observations"] = [x["id"] for x in
                FollowupAutomationAuthority(store).observe_planner(
                    context=state["reflection_followup_context"], stored_plan=stored, full_state=state,
                    call=call, prompt=prompt)]
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


__all__ = [
    "MAX_COST_USD", "MIN_REPLAN_SECONDS", "SETTLEMENT_REPLAN_SECONDS", "build_state", "main",
    "replan_pacing_hold", "run_planner",
]
