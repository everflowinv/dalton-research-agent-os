"""Q2 / C4: one record a week about where the research cycle spent itself.

The system has plenty of evidence about *what* it found and almost none about
*how it spent the week finding it*.  Cost lands in the observability tables,
questions land in the backlog, retired Claims land in the retirement authority,
planner inquiries land inside a JSON column of a research plan, and nothing
ever reads the five of them side by side.  So nobody -- machine or human --
can answer the question the owner's Monday meeting actually opens with: *我们
把时间花在哪*.

This module answers it, and does nothing else.  The v0.4 freeze is kept
literally:

- it **never writes the Ledger**.  It opens no Claim authority, no deliverable
  authority, no mission authority.  Its only write is one row in its own table.
- it **never changes policy**.  Its output includes ``policy_suggestions``,
  which are sentences.
- it **never admits a question**.  Its output includes ``backlog_candidates``,
  which are question text plus a ``because`` plus refs, for the planner or a
  human to pick up through the backlog's own admission path.

Everything here is deterministic.  There is no model call, which is why the
lane costs nothing and why a reflection can be recomputed and compared: two
runs over the same week read the same rows and hash to the same
``inputs_hash``, and the second one is a ``duplicate``.

What it cannot see, it says it cannot see.  Three of the eight metrics are
partly or wholly unavailable on today's Core -- the tick summary is never
persisted, C2's budget pools do not exist, and P14e has not landed -- and each
of those reports ``available: false`` with the reason rather than a zero.  A
zero and an absence look identical in a table and mean opposite things.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .store import DaltonStore, authorization_flag, authorized_flag, content_hash

SCHEMA_VERSION = "0.1"
_SCHEMA_PATH = Path(__file__).with_name("research_cycle_reflection_schema.sql")

# Bumped when a metric changes what it counts.  Part of ``inputs_hash``, so a
# fixed counter re-reflects a week that a broken one already reflected on.
# 0.2: W4 added the judgement-outcome counts, so a week already reflected
# on under 0.1 is re-reflected once rather than reading as unchanged.
REFLECTION_VERSION = "0.2"

# The write scope a reflection belongs to.  It is a deliverable-class artefact:
# a dated document about the mission, produced on a cadence, read by a human.
# It is not a Claim and it is not an observation, and giving it its own scope
# would ask the owner to publish a mission version for a record that asserts
# nothing about any company.
WRITE_SCOPE = "deliverable"

MAX_BACKLOG_CANDIDATES = 6
MAX_POLICY_SUGGESTIONS = 6
MAX_QUESTION_CHARS = 400
MAX_BECAUSE_CHARS = 600
MAX_REFS = 12
MAX_TABLE_ROWS = 20

# A tick counts as idle when every lane it drove reported one of these.  A lane
# that was never installed ("unconfigured") does not make a tick busy and does
# not make it idle either -- it was not asked to do anything -- so it is not
# here and not counted against the tick.
IDLE_LANE_STATUSES: frozenset[str] = frozenset({"idle", "skipped"})

# The prefix a work order takes from the code that builds it, e.g.
# ``work:document-extraction-<digest>`` -> ``document-extraction``.  Until C2
# gives ``LaneSpec`` a ``budget_pool`` and stamps it into the work order, this
# prefix is the only per-producer attribution the Core carries, so it is what a
# pool is made of.  See ``policy_suggestions``: this should stop being a
# prefix-parse.
_WORK_ORDER_FAMILY_RE = re.compile(r"^work(?:-order)?:([a-z0-9-]+?)(?:[:-][0-9a-f]{8,}.*)?$")

# Which registered lane a work-order family belongs to, where the code says so
# rather than where the name suggests it.  ``document-numeric`` and
# ``metric-discovery`` are built inside ``document_extraction``'s own child, so
# they are that lane's spend even though they read like separate ones.
# Everything absent from this map is reported by family with ``lane: null``:
# an unverified guess about who spent the money is worse than an honest gap.
WORK_ORDER_FAMILY_LANES: Mapping[str, str] = {
    "document-extraction": "document_extraction",
    "document-numeric": "document_extraction",
    "metric-discovery": "document_extraction",
    "llm-research-planner": "research_plan",
    "research-plan": "research_plan",
    "plan": "research_plan",
}


class ResearchCycleReflectionError(RuntimeError):
    """Base error for the weekly reflection."""


class ResearchCycleReflectionValidationError(ResearchCycleReflectionError):
    """An argument does not satisfy the closed contract."""


class ResearchCycleReflectionConflict(ResearchCycleReflectionError):
    """An append-only record was reused with different semantics."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value: Any, name: str, *, maximum: int = 512, minimum: int = 1) -> str:
    if not isinstance(value, str) or not (minimum <= len(value.strip()) <= maximum):
        raise ResearchCycleReflectionValidationError(
            f"{name} must be text of {minimum}..{maximum} characters"
        )
    return value.strip()


# ---------------------------------------------------------------------------
# the week
# ---------------------------------------------------------------------------


def _as_utc(value: Any) -> datetime | None:
    """A Core timestamp as an aware UTC datetime, or None if unreadable.

    The Ledger holds both ``...+00:00`` and local-offset stamps (the first
    weekly brief was published with ``-04:00``), and a few rows predate the
    convention entirely.  A row whose time cannot be read is not silently
    dropped into the window; it is counted as outside it, and the metric that
    counts it says how many it could not read.
    """

    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def iso_week_label(moment: datetime) -> str:
    """``2026-W37``: the ISO week a moment falls in."""

    year, week, _ = moment.isocalendar()
    return f"{year:04d}-W{week:02d}"


def closed_week(now: datetime) -> dict[str, Any]:
    """The week that had closed by ``now``, in ``now``'s own timezone.

    The lane fires on the first tick after Monday 00:00 local, and what it
    reflects on is the week that boundary ended -- last Monday 00:00 to this
    Monday 00:00.  Reflecting on the week in progress would produce a record
    that is wrong by Wednesday and a duplicate rule that never settles.

    Local, not UTC, because the boundary is a person's Monday morning.  The
    stored window is the same instants written in UTC, so the comparison
    against Ledger timestamps needs no timezone table.
    """

    if now.tzinfo is None:
        now = now.astimezone()
    # Arithmetic on the calendar, then one conversion back to an instant.
    # Subtracting seven days from an aware datetime moves the instant, so
    # across a DST boundary the "Monday 00:00" it lands on is 23:00 or 01:00 --
    # and the window silently gains or loses an hour of the week it reports on.
    # ``date`` has no offset to lose, so the boundary is a calendar fact and
    # the offset is applied to it afterwards.
    today = now.date()
    this_monday_date = today - timedelta(days=today.weekday())
    start_date = this_monday_date - timedelta(days=7)
    zone = now.tzinfo
    this_monday = datetime.combine(this_monday_date, time.min).replace(tzinfo=zone)
    start = datetime.combine(start_date, time.min).replace(tzinfo=zone)
    return {
        "iso_week": iso_week_label(start),
        "start": start.astimezone(timezone.utc).isoformat(timespec="microseconds"),
        "end": this_monday.astimezone(timezone.utc).isoformat(timespec="microseconds"),
        "start_local": start.isoformat(timespec="seconds"),
        "end_local": this_monday.isoformat(timespec="seconds"),
        "days": 7,
        # The window is seven calendar days; across a DST boundary it is 167 or
        # 169 hours, and the cap arithmetic uses the days.
        "hours": round((this_monday - start).total_seconds() / 3600, 3),
    }


def _in_window(value: Any, window: Mapping[str, Any]) -> bool:
    moment = _as_utc(value)
    if moment is None:
        return False
    start = _as_utc(window["start"])
    end = _as_utc(window["end"])
    return start is not None and end is not None and start <= moment < end


# ---------------------------------------------------------------------------
# reading the Core
#
# Every read is defensive in the same way and for the same reason: this Core is
# a moving target.  Three of the tables below did not exist a week ago and two
# will not exist until Wave 2, and a reflection that raises when an authority
# has not been installed is a reflection that stops working every time the
# system grows.  A missing table is an ``available: false`` with the table's
# name in the reason.
# ---------------------------------------------------------------------------


def _table_exists(core: sqlite3.Connection, name: str) -> bool:
    row = core.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _unavailable(reason: str, **extra: Any) -> dict[str, Any]:
    return {"available": False, "reason": reason, **extra}


def _rows(core: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
    core.row_factory = sqlite3.Row
    return core.execute(sql, tuple(params)).fetchall()


def work_order_family(ref: Any) -> str:
    """The producer a work order ref names, or ``unattributed``."""

    match = _WORK_ORDER_FAMILY_RE.match(str(ref or "").strip())
    return match.group(1) if match else "unattributed"


def spend_by_pool(
    core: sqlite3.Connection, window: Mapping[str, Any], *, budget: Mapping[str, Any] | None
) -> dict[str, Any]:
    """What each pool spent this week, and against what cap.

    A pool is a work-order family (see ``WORK_ORDER_FAMILY_LANES``).  There is
    no per-pool cap to compare it against, because C2 has not landed: the
    mission budget holds one number, ``max_daily_cost_usd``, for everything.
    So the cap reported is the mission's, the window's worth of it, and every
    pool's share of it -- which is exactly the number C2 needs in order to
    choose the pool weights, and exactly the number a reader would otherwise
    invent.
    """

    for table in ("observability_cost_entries", "observability_usage_entries", "model_invocations"):
        if not _table_exists(core, table):
            return _unavailable(f"{table} 不在这个 Core 里，模型花费无法归集", pools=[])
    rows = _rows(core, """
        SELECT mi.work_order_ref AS work_order_ref, mi.capability AS capability,
               mi.model_family AS model_family, ce.amount_micros AS amount_micros,
               ce.cost_status AS cost_status, ce.created_at AS created_at
        FROM observability_cost_entries ce
        JOIN observability_usage_entries ue ON ue.usage_entry_id = ce.usage_entry_ref
        JOIN model_invocations mi ON mi.invocation_id = ue.invocation_ref
    """)
    pools: dict[str, dict[str, Any]] = {}
    total_micros = 0
    calls = 0
    unpriced = 0
    for row in rows:
        if not _in_window(row["created_at"], window):
            continue
        family = work_order_family(row["work_order_ref"])
        pool = pools.setdefault(family, {
            "pool": family,
            "lane": WORK_ORDER_FAMILY_LANES.get(family),
            "calls": 0,
            "cost_micros": 0,
            "capabilities": [],
        })
        pool["calls"] += 1
        calls += 1
        amount = row["amount_micros"]
        if amount is None:
            unpriced += 1
        else:
            pool["cost_micros"] += int(amount)
            total_micros += int(amount)
        capability = row["capability"]
        if capability and capability not in pool["capabilities"]:
            pool["capabilities"].append(str(capability))
    cap_usd = None
    if budget is not None and budget.get("max_daily_cost_usd") is not None:
        cap_usd = round(float(budget["max_daily_cost_usd"]) * int(window["days"]), 6)
    ordered = sorted(pools.values(), key=lambda item: (-item["cost_micros"], item["pool"]))
    for pool in ordered:
        pool["cost_usd"] = round(pool["cost_micros"] / 1_000_000, 6)
        pool["share_of_spend"] = (
            round(pool["cost_micros"] / total_micros, 4) if total_micros else 0.0
        )
        pool["share_of_mission_cap"] = (
            None if not cap_usd else round(pool["cost_usd"] / cap_usd, 6)
        )
    return {
        "available": True,
        "pool_model": "work_order_family",
        "pool_model_reason": (
            "C2 未落地：LaneSpec 没有 budget_pool，mission budget 只有一个 max_daily_cost_usd。"
            "在那之前一条 work order family 自成一池，lane 归属只在代码能证明的地方标注。"
        ),
        "pools": ordered[:MAX_TABLE_ROWS],
        "pool_count": len(ordered),
        "total_cost_usd": round(total_micros / 1_000_000, 6),
        "calls": calls,
        "unpriced_calls": unpriced,
        "mission_window_cap_usd": cap_usd,
        "share_of_mission_cap": (
            None if not cap_usd else round(total_micros / 1_000_000 / cap_usd, 6)
        ),
    }


def backlog_movement(core: sqlite3.Connection, window: Mapping[str, Any]) -> dict[str, Any]:
    """New questions registered this week against questions answered."""

    if not _table_exists(core, "backlog_question_events"):
        return _unavailable("backlog_question_events 不在这个 Core 里")
    rows = _rows(core, "SELECT question_ref, state, reason, created_at FROM backlog_question_events")
    by_state: dict[str, list[str]] = {}
    unreadable = 0
    for row in rows:
        if _as_utc(row["created_at"]) is None:
            unreadable += 1
            continue
        if not _in_window(row["created_at"], window):
            continue
        by_state.setdefault(str(row["state"]), []).append(str(row["question_ref"]))
    # The head state *as of the end of the window*, and by instant rather than
    # by the order rows came back in. Two events a millisecond apart on
    # different offsets sort the wrong way as text, and "what was still open on
    # Sunday night" is not "what is open now": a question answered on Tuesday
    # was open for the whole week being reported on.
    end = _as_utc(window["end"])
    heads: dict[str, tuple[datetime, str]] = {}
    for row in rows:
        moment = _as_utc(row["created_at"])
        if moment is None or (end is not None and moment >= end):
            continue
        ref = str(row["question_ref"])
        seen = heads.get(ref)
        if seen is None or moment >= seen[0]:
            heads[ref] = (moment, str(row["state"]))
    open_states = {"open", "selected", "planned", "in_progress"}
    return {
        "available": True,
        "new_questions": len(by_state.get("open", [])),
        "answered": len(by_state.get("answered", [])),
        "blocked": len(by_state.get("blocked", [])),
        "retired": len(by_state.get("retired", [])),
        "moved_by_state": {state: len(refs) for state, refs in sorted(by_state.items())},
        "new_question_refs": sorted(set(by_state.get("open", [])))[:MAX_REFS],
        "answered_refs": sorted(set(by_state.get("answered", [])))[:MAX_REFS],
        "open_at_end": sum(1 for _, state in heads.values() if state in open_states),
        "unreadable_timestamps": unreadable,
    }


def claims_retired(core: sqlite3.Connection, window: Mapping[str, Any]) -> dict[str, Any]:
    """Claims the system took back this week, and challenges still open."""

    if not _table_exists(core, "claim_retirement_decisions"):
        return _unavailable("claim_retirement_decisions 不在这个 Core 里")
    decisions = _rows(core, (
        "SELECT claim_version_ref, decision, created_at FROM claim_retirement_decisions"
    ))
    # 2026-09-24: a retirement a later record withdrew is not "taken back";
    # the withdrawals are counted on their own.
    from .claim_retirement import reinstated_claim_version_refs

    reinstated = reinstated_claim_version_refs(core)
    retired = [row for row in decisions
               if row["decision"] == "retired" and _in_window(row["created_at"], window)
               and str(row["claim_version_ref"]) not in reinstated]
    kept = [row for row in decisions
            if row["decision"] == "kept" and _in_window(row["created_at"], window)]
    challenges_raised = 0
    open_challenges = 0
    if _table_exists(core, "claim_retirement_challenges"):
        decided = {str(row["claim_version_ref"]) for row in decisions}
        for row in _rows(core, (
            "SELECT claim_version_ref, created_at FROM claim_retirement_challenges"
        )):
            if _in_window(row["created_at"], window):
                challenges_raised += 1
            if str(row["claim_version_ref"]) not in decided:
                open_challenges += 1
    return {
        "available": True,
        "retired": len(retired),
        "reinstated": len(reinstated),
        "kept": len(kept),
        "challenges_raised": challenges_raised,
        "challenges_open_at_end": open_challenges,
        "retired_refs": sorted(str(row["claim_version_ref"]) for row in retired)[:MAX_REFS],
    }


# What P14e requires before an inquiry can become a ResearchTask.  Both are
# versioned owner acts, which is why a zero here is reported with which of them
# is missing rather than as a flat "not dispatched".
RESEARCH_TASK_SCOPE = "research_task"


def planner_inquiries(
    core: sqlite3.Connection,
    window: Mapping[str, Any],
    *,
    write_scopes: Sequence[str] = (),
) -> dict[str, Any]:
    """How many questions the planner raised, and how many were dispatched.

    Dispatch means a ``BoundedPlannerLoop`` was admitted **from an inquiry** --
    D1's ``ResearchTask``, which P14e landed.  A loop a human opened is not a
    dispatch, so the count reads ``admission.source`` rather than counting
    loops, and the two are reported side by side.

    A zero is reported with the reason, and the reason names which of P14e's
    two gates is shut: *the planner asked eleven questions this week and
    nothing was sent to answer any of them* is the finding, not a missing row.
    """

    if not _table_exists(core, "coverage_mission_research_plans"):
        return _unavailable("coverage_mission_research_plans 不在这个 Core 里")
    plans = _rows(core, (
        "SELECT plan_id, mission_version_ref, inquiries_json, work_order_ref, created_at "
        "FROM coverage_mission_research_plans"
    ))
    total = 0
    plan_count = 0
    questions: list[str] = []
    for row in plans:
        if not _in_window(row["created_at"], window):
            continue
        plan_count += 1
        try:
            inquiries = json.loads(row["inquiries_json"] or "[]")
        except (TypeError, ValueError):
            continue
        if not isinstance(inquiries, list):
            continue
        total += len(inquiries)
        for item in inquiries:
            if isinstance(item, Mapping) and item.get("question"):
                questions.append(str(item["question"])[:MAX_QUESTION_CHARS])
    dispatched = 0
    human_loops = 0
    dispatch_reason = None
    granted = RESEARCH_TASK_SCOPE in set(write_scopes)
    templates = 0
    if _table_exists(core, "bounded_probe_template_versions"):
        templates = int(_rows(
            core, "SELECT COUNT(*) AS n FROM bounded_probe_template_versions"
        )[0]["n"])
    if _table_exists(core, "bounded_planner_loop_versions"):
        loops = _rows(core, (
            "SELECT record_json, created_at FROM bounded_planner_loop_versions "
            "WHERE version_number = 1"
        ))
        for row in loops:
            if not _in_window(row["created_at"], window):
                continue
            try:
                record = json.loads(row["record_json"])
            except (TypeError, ValueError):
                record = {}
            source = ((record.get("admission") or {}).get("source"))
            if source == "inquiry":
                dispatched += 1
            else:
                human_loops += 1
        if dispatched == 0:
            missing = []
            if not granted:
                missing.append(f"mission 的 may_write 没有授予 {RESEARCH_TASK_SCOPE}")
            if not templates:
                missing.append("还没有发布任何 ad-hoc ProbeTemplate")
            dispatch_reason = (
                "本周没有从 inquiry 开出任何 BoundedPlannerLoop。"
                + ("P14e 的两道闸里，" + "、".join(missing) + "；两者都是 owner 的版本化动作。"
                   if missing else
                   "P14e 的两道闸都开着，所以要么本周没有可派发的 inquiry，"
                   "要么当日的 ad-hoc 池已经用尽（lane 会报 skipped:pool_exhausted）。")
            )
    else:
        dispatch_reason = "bounded_planner_loop_versions 不在这个 Core 里，派发数按 0 计"
    return {
        "available": True,
        "plans_recorded": plan_count,
        "inquiries_raised": total,
        "dispatched": dispatched,
        # A loop a person opened answers a question too, but it is not the
        # planner's inquiry being acted on, and folding the two together would
        # make the ad-hoc path look busier than it is.
        "human_opened_loops": human_loops,
        "research_task_granted": granted,
        "probe_templates": templates,
        "dispatch_ratio": round(dispatched / total, 4) if total else None,
        "dispatch_reason": dispatch_reason,
        "sample_questions": questions[:5],
    }


def idle_tick_ratio(tick_summaries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The share of ticks in which every lane reported nothing to do.

    The summaries have to be handed in, because the Core does not keep them:
    ``bounded_planner_driver.run_once`` returns a dict, ``service`` puts it in
    ``run/heartbeat.json``, and the next tick overwrites it.  So today this
    metric is only computable from whatever a caller archived, and when nobody
    archived anything it says so instead of reporting a comfortable zero.
    """

    if not tick_summaries:
        return _unavailable(
            "Core 里没有 tick 账本——run_once 的摘要只写进 run/heartbeat.json，"
            "下一次 tick 就覆盖它；除非有人归档过，没有东西可数",
            ticks=0,
        )
    from .lane_registry import RESERVED_DRIVER_KEYS

    ticks = 0
    idle = 0
    lane_idle: dict[str, int] = {}
    lane_seen: dict[str, int] = {}
    for summary in tick_summaries:
        if not isinstance(summary, Mapping):
            continue
        lanes = {
            key: value for key, value in summary.items()
            if key not in RESERVED_DRIVER_KEYS and isinstance(value, Mapping)
        }
        if not lanes:
            continue
        ticks += 1
        statuses = []
        for key, value in lanes.items():
            status = str(value.get("status") or "")
            lane_seen[key] = lane_seen.get(key, 0) + 1
            if status in IDLE_LANE_STATUSES:
                lane_idle[key] = lane_idle.get(key, 0) + 1
            if status:
                statuses.append(status)
        if statuses and all(status in IDLE_LANE_STATUSES for status in statuses):
            idle += 1
    if not ticks:
        return _unavailable("交上来的 tick 摘要里没有一条含 lane 结果", ticks=0)
    return {
        "available": True,
        "ticks": ticks,
        "idle_ticks": idle,
        "ratio": round(idle / ticks, 4),
        "idle_by_lane": {
            key: round(lane_idle.get(key, 0) / count, 4)
            for key, count in sorted(lane_seen.items())
        },
    }


def open_human_checkpoints(
    core: sqlite3.Connection, window: Mapping[str, Any], *, now: datetime
) -> dict[str, Any]:
    """Stages a human entered and has not closed, and how old they are.

    A checkpoint is open when the latest stage record for a (company, stage)
    pair is ``entered`` -- ``gate_passed`` and ``gate_failed`` both close it.
    Ageing is measured from that ``entered`` row, because that is the moment
    the system stopped and started waiting.
    """

    if not _table_exists(core, "coverage_mission_stage_records"):
        return _unavailable("coverage_mission_stage_records 不在这个 Core 里")
    rows = _rows(core, (
        "SELECT company_ref, stage_ref, status, actor_ref, created_at "
        "FROM coverage_mission_stage_records"
    ))
    # Ordered in Python by the parsed instant, not by SQLite's text collation.
    # The Ledger holds both "...+00:00" and local-offset stamps -- the first
    # weekly brief was published at -04:00 -- and sorted as text, a stamp
    # written in a western offset sorts *after* a later UTC one. Getting this
    # wrong reads a closed checkpoint as still open.
    latest: dict[tuple[str, str], tuple[datetime, sqlite3.Row]] = {}
    for row in rows:
        moment = _as_utc(row["created_at"])
        if moment is None:
            continue
        key = (str(row["company_ref"]), str(row["stage_ref"]))
        seen = latest.get(key)
        if seen is None or moment >= seen[0]:
            latest[key] = (moment, row)
    open_rows = []
    for (company_ref, stage_ref), (_, row) in sorted(latest.items()):
        if str(row["status"]) != "entered":
            continue
        entered = _as_utc(row["created_at"])
        age_days = None if entered is None else round(
            (now.astimezone(timezone.utc) - entered).total_seconds() / 86400, 2
        )
        open_rows.append({
            "company_ref": company_ref, "stage_ref": stage_ref,
            "entered_at": row["created_at"], "age_days": age_days,
        })
    open_rows.sort(key=lambda item: (-(item["age_days"] or 0), item["stage_ref"]))
    closed_this_week = sum(
        1 for row in rows
        if str(row["status"]) in {"gate_passed", "gate_failed"}
        and _in_window(row["created_at"], window)
    )
    ages = [row["age_days"] for row in open_rows if row["age_days"] is not None]
    return {
        "available": True,
        "open": len(open_rows),
        "oldest_age_days": max(ages) if ages else None,
        "median_age_days": (sorted(ages)[len(ages) // 2] if ages else None),
        "closed_this_week": closed_this_week,
        "checkpoints": open_rows[:MAX_TABLE_ROWS],
        "note": (
            "「人类检查点」在今天的 Core 里没有独立的表：它是 mission 的 "
            "autonomy.human_checkpoints 词表加上 stage record 的 entered / gate_passed / "
            "gate_failed 三态。这里数的是所有停在 entered 的 stage。"
        ),
    }


def quality_scores_published(core: sqlite3.Connection, window: Mapping[str, Any]) -> dict[str, Any]:
    """``QualityScoreVersion`` rows written this week, by rubric."""

    if not _table_exists(core, "research_quality_score_versions"):
        return _unavailable(
            "本研究项目尚未启用研究质量评分，请管理员检查评分服务。"
        )
    rows = _rows(core, (
        "SELECT rubric_ref, artefact_kind, judge_status, target_ref, created_at "
        "FROM research_quality_score_versions"
    ))
    by_rubric: dict[str, int] = {}
    judged = 0
    deterministic_only = 0
    targets: set[str] = set()
    for row in rows:
        if not _in_window(row["created_at"], window):
            continue
        rubric = str(row["rubric_ref"])
        by_rubric[rubric] = by_rubric.get(rubric, 0) + 1
        targets.add(str(row["target_ref"]))
        if row["judge_status"] == "scored":
            judged += 1
        elif row["judge_status"] is None:
            deterministic_only += 1
    return {
        "available": True,
        "published": sum(by_rubric.values()),
        "by_rubric": dict(sorted(by_rubric.items())),
        "distinct_targets": len(targets),
        "judged": judged,
        "deterministic_only": deterministic_only,
    }


def journal_feedback(core: sqlite3.Connection, window: Mapping[str, Any]) -> dict[str, Any]:
    """The five feedback words, counted, from both places they are written.

    ``AnalystJournalEntry`` and ``weekly_brief_feedback`` use the same closed
    vocabulary and mean the same thing, so they are counted together and named
    apart.  Zero of both is the interesting reading, and it is the current one.
    """

    words = ("read", "useful", "needs_more_evidence", "disagree", "revise")
    by_word = {word: 0 for word in words}
    sources: dict[str, Any] = {}
    missing: list[str] = []
    for table, label in (
        ("analyst_journal_entries", "analyst_journal"),
        ("weekly_brief_feedback", "weekly_brief"),
    ):
        if not _table_exists(core, table):
            sources[label] = {"available": False, "reason": f"{table} 不在这个 Core 里"}
            missing.append(table)
            continue
        count = 0
        for row in _rows(core, f"SELECT verdict, created_at FROM {table}"):
            if not _in_window(row["created_at"], window):
                continue
            verdict = str(row["verdict"])
            if verdict in by_word:
                by_word[verdict] += 1
            count += 1
        sources[label] = {"available": True, "entries": count}
    total = sum(by_word.values())
    if len(missing) == len(sources):
        # Both places feedback is written are absent. Reporting "0 条反馈" here
        # would say the owner read and had no comment, when what happened is
        # that there was nowhere to put a comment.
        return _unavailable(
            "两处反馈表都不在这个 Core 里（" + "、".join(missing) + "），"
            "「没有反馈」读不出任何东西",
            by_word=by_word, sources=sources,
        )
    return {
        "available": True,
        # Partial when one of the two is absent: the count is real, but it is
        # a count of one of the two places a verdict can land.
        "partial": bool(missing),
        "missing_sources": missing,
        "entries": total,
        "by_word": by_word,
        "sources": sources,
        "outstanding_words": sum(
            by_word[word] for word in ("needs_more_evidence", "disagree", "revise")
        ),
    }


def judgement_outcomes(core: sqlite3.Connection, window: Mapping[str, Any]) -> dict[str, Any]:
    """W4 / Chem §3.3: how the judgements we already made turned out.

    The one metric here that is not about *this* week's work.  It is about
    whether the work of every earlier week was right, which is the question
    Chem's 89-out-of-92 ``NO_CHANGE`` could never be asked.  Two readings, and
    both are wanted:

    ``this_week``
        checks whose newest version landed inside the window -- the ones that
        *changed their mind* about a past decision this week, usually because
        the window finally had enough settled sessions in it.
    ``to_date``
        the newest version of every check, ever.  This is the ledger; the
        weekly slice is the news.

    Nothing is computed here: the rows were derived by
    ``judgement_outcome.build_outcome_checks`` under a frozen formula and this
    only counts them.  A Core where the zero-base lane has never run reports
    ``available: false`` with the reason rather than a page of zeroes, because
    "no judgement was ever wrong" and "nobody has looked" are opposite
    findings that a table of zeroes renders identically.
    """

    if not _table_exists(core, "judgement_outcome_check_pointer"):
        return _unavailable(
            "judgement_outcome_check_pointer 不在这个 Core 里：W4 的判断结果台账还没有部署"
        )
    from .judgement_outcome import FORMULA_REF, OUTCOMES

    rows = _rows(core, (
        "SELECT p.outcome AS outcome, p.check_kind AS check_kind, "
        "p.company_ref AS company_ref, v.created_at AS created_at, "
        "v.formula_ref AS formula_ref "
        "FROM judgement_outcome_check_pointer p "
        "JOIN judgement_outcome_check_versions v ON v.version_id = p.version_id"
    ))
    if not rows:
        return _unavailable(
            "判断结果台账是空的：零基复盘 lane 还没有跑过一次，或者还没有可评的判断",
            checked=0,
        )
    to_date = {outcome: 0 for outcome in OUTCOMES}
    this_week = {outcome: 0 for outcome in OUTCOMES}
    by_kind = {"no_change": 0, "revise": 0}
    formulas: set[str] = set()
    companies: set[str] = set()
    for row in rows:
        outcome = str(row["outcome"])
        if outcome in to_date:
            to_date[outcome] += 1
            if _in_window(row["created_at"], window):
                this_week[outcome] += 1
        kind = str(row["check_kind"])
        if kind in by_kind:
            by_kind[kind] += 1
        formulas.add(str(row["formula_ref"]))
        companies.add(str(row["company_ref"]))
    return {
        "available": True,
        "checked": len(rows),
        "companies": len(companies),
        "by_kind": by_kind,
        "to_date": to_date,
        "this_week": this_week,
        # The two the owner asked for, promoted so a reader does not have to
        # know the whole vocabulary to find them.
        "should_have_moved": to_date["should_have_moved"],
        "moved_right": to_date["moved_right"],
        # A pass under an older formula is legible as such rather than being
        # silently mixed in with the current one.
        "formula_refs": sorted(formulas),
        "current_formula_ref": FORMULA_REF,
        "stale_formula": sorted(formulas - {FORMULA_REF}),
    }


# ---------------------------------------------------------------------------
# the metrics, together
# ---------------------------------------------------------------------------


def compute_metrics(
    core: sqlite3.Connection,
    *,
    window: Mapping[str, Any],
    budget: Mapping[str, Any] | None = None,
    write_scopes: Sequence[str] = (),
    tick_summaries: Sequence[Mapping[str, Any]] = (),
    now: datetime | None = None,
) -> dict[str, Any]:
    """Eight readings of one week.  No writes, no model, no opinions."""

    moment = now or datetime.now(timezone.utc)
    return {
        "spend": spend_by_pool(core, window, budget=budget),
        "backlog": backlog_movement(core, window),
        "claims": claims_retired(core, window),
        "planner": planner_inquiries(core, window, write_scopes=write_scopes),
        "ticks": idle_tick_ratio(tick_summaries),
        "human_checkpoints": open_human_checkpoints(core, window, now=moment),
        "quality_scores": quality_scores_published(core, window),
        "journal": journal_feedback(core, window),
        "judgement_outcomes": judgement_outcomes(core, window),
    }


def inputs_hash(metrics: Mapping[str, Any], window: Mapping[str, Any]) -> str:
    """What makes two reflections on one week the same reflection.

    Every number the reflection read, plus the window and the counter version.
    Not the narrative and not the candidates: those are derived, so hashing
    them would only make the duplicate rule depend on its own output.  The
    ageing of an open checkpoint is deliberately excluded -- it moves every
    second by definition, and a duplicate rule that never fires is not one.
    """

    scrubbed = json.loads(json.dumps(metrics, ensure_ascii=False, sort_keys=True))
    checkpoints = scrubbed.get("human_checkpoints")
    if isinstance(checkpoints, dict):
        checkpoints.pop("oldest_age_days", None)
        checkpoints.pop("median_age_days", None)
        for row in checkpoints.get("checkpoints") or []:
            if isinstance(row, dict):
                row.pop("age_days", None)
    return content_hash({
        "reflection_version": REFLECTION_VERSION,
        "window": {"start": window["start"], "end": window["end"]},
        "metrics": scrubbed,
    })


# ---------------------------------------------------------------------------
# 我们把时间花在哪
# ---------------------------------------------------------------------------

NARRATIVE_TITLE = "我们把时间花在哪"


def _usd(value: Any) -> str:
    return "—" if value is None else f"${float(value):.2f}"


def _share(value: Any) -> str:
    """A share, with "不到 0.1%" instead of a 0.0% that means "some"."""

    if value is None:
        return "—"
    share = float(value)
    if 0 < share < 0.001:
        return "不到 0.1%"
    return f"{share:.1%}"


# 2026-10-08 inspection (DEFECT-06/07): the owner reads this on a Monday.  The
# first templates said what the code saw -- ``mission``, ``lane``, ``Claim 层``,
# ``inquiry``, ``BoundedPlannerLoop``, ``ad-hoc ProbeTemplate``, ``run_once``,
# ``run/heartbeat.json`` -- which is a developer's log, not a research
# director's week.  The prose, the candidates and the suggestions below are
# plain Chinese about research; every identifier and every engineering note
# moved to ``technical_notes`` (shown under 技术详情), and the metrics, which
# carry the exact reasons, are unchanged.

#: What each work-order family is, for the owner.  Unknown families are
#: 「其他研究工作」 with the family under 技术详情.
WORK_ORDER_FAMILY_LABELS: Mapping[str, str] = {
    "document-extraction": "文档数据抽取",
    "document-numeric": "文档数字核对",
    "metric-discovery": "指标发现",
    "llm-research-planner": "研究计划",
    "research-plan": "研究计划",
    "plan": "研究计划",
    "mission-document-research": "文档深度研究",
    "mission-document-authority-rebind": "文档来源重新核对",
    "mission-document-recovery": "文档补救重试",
    "alphaengine-doc": "卖方研报与电话会获取",
    "unattributed": "未归类的模型调用",
}
#: The research stages a checkpoint can stop at, as the cockpit names them.
STAGE_LABELS: Mapping[str, str] = {
    "initial_screen": "初步筛选", "deep_insight_gate": "深度研究",
    "industry_model": "行业模型", "company_model": "公司模型",
    "investment_memo": "投资备忘录", "active_coverage": "持续覆盖",
}
FEEDBACK_WORD_LABELS: Mapping[str, str] = {
    "read": "已读", "useful": "有用", "needs_more_evidence": "需要更多证据",
    "disagree": "不同意", "revise": "需要修改",
}
OWNER_AUTHORITY_NOTE = (
    "这份复盘只做统计：不会修改任何研究结论、设置或问题清单。"
    "下面的候选问题和建议要由你或研究规划确认后才会执行。"
)


def pool_label(family: Any) -> str:
    return WORK_ORDER_FAMILY_LABELS.get(str(family or ""), "其他研究工作")


def _company_text(company_ref: Any) -> str:
    ref = str(company_ref or "")
    if ref.startswith("company:ticker:"):
        return ref.rsplit(":", 1)[-1].upper()
    return "一家公司"


def _dispatch_owner_reason(planner: Mapping[str, Any]) -> str | None:
    """Why no question was sent to research, in the owner's words."""

    if planner.get("dispatched") or not planner.get("inquiries_raised"):
        return None
    missing = []
    if not planner.get("research_task_granted"):
        missing.append("研究任务还没有授权系统自行开展专项研究")
    if not planner.get("probe_templates"):
        missing.append("还没有设置专项研究的模板")
    if missing:
        return ("这些问题还没有安排专项研究，因为" + "，并且".join(missing)
                + ("；两项都需要你在研究任务设置里开启。" if len(missing) > 1
                   else "；这需要你在研究任务设置里开启。"))
    return "专项研究已经开启，但本周没有可以安排的问题，或者当天的专项研究预算已经用完。"


def narrative(metrics: Mapping[str, Any], window: Mapping[str, Any]) -> dict[str, Any]:
    """Plain prose plus a table, both derived from the metrics and nothing else.

    Prose rather than a chart because the reader is a person opening a Monday
    meeting, and a table rather than only prose because the question is
    quantitative.  Every sentence below is a rendering of a number that is in
    the record beside it; none of them is a judgement, and none of them was
    written by a model.
    """

    spend = metrics["spend"]
    backlog = metrics["backlog"]
    claims = metrics["claims"]
    planner = metrics["planner"]
    ticks = metrics["ticks"]
    checkpoints = metrics["human_checkpoints"]
    scores = metrics["quality_scores"]
    journal = metrics["journal"]
    week = window["iso_week"]

    table: list[dict[str, Any]] = []
    if spend.get("available"):
        for pool in spend["pools"]:
            table.append({
                "pool": pool["pool"],
                "pool_label": pool_label(pool["pool"]),
                "lane": pool["lane"] or "—",
                "cost_usd": pool["cost_usd"],
                "calls": pool["calls"],
                "share_of_spend": pool["share_of_spend"],
                "share_of_mission_cap": pool["share_of_mission_cap"],
            })

    lines: list[str] = []
    if spend.get("available"):
        if spend["calls"] == 0:
            lines.append(f"这一周（{week}）没有任何计费的模型调用：系统在运行，但没有花钱。")
        else:
            top = spend["pools"][0]
            share = spend.get("share_of_mission_cap")
            cap_clause = (
                "" if share is None else
                f"，占本研究任务本周预算上限（{_usd(spend['mission_window_cap_usd'])}）的 {_share(share)}"
            )
            lines.append(
                f"这一周（{week}）模型调用共 {spend['calls']} 次，花费 "
                f"{_usd(spend['total_cost_usd'])}{cap_clause}。"
                f"花得最多的是「{pool_label(top['pool'])}」，{_usd(top['cost_usd'])}，"
                f"占本周花费的 {top['share_of_spend']:.0%}。"
            )
            if spend["pool_count"] == 1:
                lines.append("本周的花费全部来自这一项工作。")
    else:
        lines.append("本周花费暂时无法统计。")

    if backlog.get("available"):
        lines.append(
            f"待研究问题：本周新增 {backlog['new_questions']} 个，回答了 {backlog['answered']} 个，"
            f"周末仍有 {backlog['open_at_end']} 个没有解决。"
            + ("未解决的问题比一周前多了。"
               if backlog["new_questions"] > backlog["answered"] else
               ("未解决的问题没有增加。"
                if backlog["answered"] else "本周既没有新增问题，也没有回答问题。"))
        )
    if claims.get("available"):
        lines.append(
            f"结论复核：本周有 {claims['retired']} 条早期结论经复核后撤下，"
            f"{claims['kept']} 条复核后保留；新提出 {claims['challenges_raised']} 项质疑，"
            f"周末还有 {claims['challenges_open_at_end']} 项待裁定。"
        )
    if planner.get("available"):
        ratio = planner["dispatch_ratio"]
        reason = _dispatch_owner_reason(planner)
        lines.append(
            f"研究计划：本周生成了 {planner['plans_recorded']} 份研究计划，"
            f"提出 {planner['inquiries_raised']} 个待核实的问题，其中 {planner['dispatched']} 个已安排专项研究"
            + ("。" if ratio is None else f"（{ratio:.0%}）。")
            + (reason or "")
        )
    if ticks.get("available"):
        lines.append(
            f"后台共运行 {ticks['ticks']} 轮，其中 {ticks['idle_ticks']} 轮没有工作可做"
            f"（{ticks['ratio']:.0%}）。"
        )
    else:
        lines.append("后台空闲比例暂时无法统计。")
    if checkpoints.get("available") and checkpoints["open"]:
        lines.append(
            f"有 {checkpoints['open']} 个研究阶段在等你审批，"
            f"最久的已经等了 {checkpoints['oldest_age_days']} 天；"
            f"本周处理了 {checkpoints['closed_this_week']} 个。"
        )
    if scores.get("available") and not scores["published"]:
        lines.append("质量评分：本周没有为任何研究产出打分。")
    elif scores.get("available"):
        lines.append(
            f"质量评分：本周为 {scores['distinct_targets']} 份研究产出打了 {scores['published']} 次分"
            f"（{scores['judged']} 次含评审意见，{scores['deterministic_only']} 次只有自动检查）。"
        )
    else:
        lines.append("质量评分暂时无法读取。")
    if not journal.get("available"):
        lines.append("你的反馈记录暂时无法读取。")
    elif journal["entries"]:
        words = "、".join(
            f"{FEEDBACK_WORD_LABELS.get(word, word)} {count} 条"
            for word, count in journal["by_word"].items() if count
        )
        partial = "（只统计到两处反馈记录中的一处）" if journal.get("partial") else ""
        lines.append(f"本周收到你 {journal['entries']} 条反馈{partial}：{words}。")
    else:
        partial = "（只统计到两处反馈记录中的一处）" if journal.get("partial") else ""
        lines.append(f"这一周没有收到你的反馈{partial}。")
    outcomes = metrics.get("judgement_outcomes") or {}
    if outcomes.get("available"):
        # Chem §3.3, in one sentence: 不动也要能被评价. Cumulative rather than
        # weekly, because a decision made in March is graded by what happened
        # in April, and a weekly-only line would report almost always zero.
        to_date = outcomes["to_date"]
        lines.append(
            f"历史判断跟踪：已评估 {outcomes['checked']} 条判断，"
            f"其中 {to_date['should_have_moved']} 条可能是「当时该调整却没有调整」，"
            f"{to_date['moved_right']} 条调整正确，"
            f"{to_date['held']} 条维持不变是对的，"
            f"{to_date['pending'] + to_date['unavailable']} 条暂时还无法评估。"
            "这些只是复核线索，不是绩效考核。"
        )
    elif outcomes:
        lines.append("历史判断跟踪暂时无法读取。")
    return {"title": NARRATIVE_TITLE, "prose": "\n".join(lines), "table": table[:MAX_TABLE_ROWS]}


def technical_notes(metrics: Mapping[str, Any]) -> list[str]:
    """The engineering reasons behind the owner's sentences, for 技术详情.

    Unavailable readings say why in terms of tables and files, and the
    attribution and scheduling gaps are named here, where a developer will
    look, instead of in the owner's prose.
    """

    out: list[str] = []
    spend = metrics.get("spend") or {}
    ticks = metrics.get("ticks") or {}
    scores = metrics.get("quality_scores") or {}
    planner = metrics.get("planner") or {}
    for name, value in metrics.items():
        if isinstance(value, Mapping) and value.get("available") is False and value.get("reason"):
            out.append(f"{name}: {value['reason']}")
    if planner.get("dispatch_reason"):
        out.append(f"planner: {planner['dispatch_reason']}")
    journal = metrics.get("journal") or {}
    if journal.get("partial"):
        out.append("journal: 只数了两处反馈表里的一处，"
                   + "、".join(journal.get("missing_sources") or []) + " 不在这个 Core 里")
    if spend.get("available") and spend.get("pool_model") == "work_order_family":
        out.append(
            "C2 的 budget_pool 应当被写进 work order 的 id（或它自己的一列），"
            "否则每一次「这条 lane 花了多少」都是一次对 ref 前缀的解析；"
            f"今天有 {sum(1 for pool in spend['pools'] if pool['lane'] is None)} 个池认不到 lane。"
        )
    if not ticks.get("available"):
        out.append(
            "tick 摘要应当落到 Core 的一张 append-only 表（每 tick 一行：状态、各 lane 状态、耗时）。"
            "现在它只写 run/heartbeat.json 并被下一次 tick 覆盖，所以闲置率、lane 卡顿、"
            "调度密度这三类问题在事后一个都答不了。"
        )
    if scores.get("available") and scores["published"] == 0:
        out.append(
            "质量分需要一条 lane 或一个发布前钩子才会被调用；"
            "Q1 的建议是先把确定性层接进发布前检查（零成本），判读层等第一次真实校准之后再定。"
        )
    return out


# ---------------------------------------------------------------------------
# candidates and suggestions -- proposals, never decisions
# ---------------------------------------------------------------------------


def _candidate(question: str, because: str, refs: Sequence[str] = ()) -> dict[str, Any]:
    return {
        "question": question[:MAX_QUESTION_CHARS],
        "because": because[:MAX_BECAUSE_CHARS],
        "refs": [str(ref) for ref in refs][:MAX_REFS],
    }


def backlog_candidates(metrics: Mapping[str, Any], window: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Questions this week's numbers raise, for someone else to admit.

    Every rule here fires on a *shape* in the metrics, not on a threshold
    somebody liked: money spent with nothing registered against it, questions
    raised and never sent, a checkpoint that has been waiting longer than the
    cadence that is supposed to clear it.  A candidate is text, a because and
    refs -- the three things ``ResearchQuestionBacklog.record_question`` asks
    for -- so a human or the planner can admit one without rewriting it.  This
    module never admits one itself.
    """

    out: list[dict[str, Any]] = []
    spend = metrics["spend"]
    backlog = metrics["backlog"]
    planner = metrics["planner"]
    ticks = metrics["ticks"]
    checkpoints = metrics["human_checkpoints"]
    scores = metrics["quality_scores"]
    journal = metrics["journal"]

    if (spend.get("available") and spend["total_cost_usd"] > 0
            and backlog.get("available") and backlog["new_questions"] == 0):
        top = spend["pools"][0] if spend["pools"] else {"pool": "?", "cost_usd": 0}
        out.append(_candidate(
            f"{window['iso_week']} 花了 {_usd(spend['total_cost_usd'])}，却没有新增任何待研究问题，"
            f"花得最多的「{pool_label(top['pool'])}」带来了什么？",
            "花费没有带来新问题，要么是在补充已有证据，要么是在重复劳动；"
            "两种都可能合理，但要说得清是哪一种。",
            [f"pool:{pool['pool']}" for pool in spend["pools"][:3]],
        ))
    if (planner.get("available") and planner["inquiries_raised"] > 0
            and planner["dispatched"] == 0):
        out.append(_candidate(
            f"系统本周提出的 {planner['inquiries_raised']} 个待核实问题都还没有安排专项研究，"
            "哪几个值得优先做？",
            _dispatch_owner_reason(planner)
            or "问题只有安排了研究才会变成证据；没有安排时它们只是记录。",
            [],
        ))
    if backlog.get("available") and backlog["new_questions"] > 3 * max(backlog["answered"], 1):
        out.append(_candidate(
            f"待研究问题本周净增 {backlog['new_questions'] - backlog['answered']} 个，"
            "哪一个最该先回答？",
            "新问题增加的速度是回答速度的三倍以上，问题清单正在变成愿望清单。",
            backlog["new_question_refs"][:5],
        ))
    if checkpoints.get("available") and (checkpoints["oldest_age_days"] or 0) >= 14:
        oldest = checkpoints["checkpoints"][0] if checkpoints["checkpoints"] else {}
        stage = STAGE_LABELS.get(str(oldest.get("stage_ref") or ""), "某个研究阶段")
        out.append(_candidate(
            f"{_company_text(oldest.get('company_ref'))}的「{stage}」阶段已经等待审批 "
            f"{checkpoints['oldest_age_days']} 天，还缺什么？",
            "审批是有意设置的停顿，不应变成长期卡住；等待超过两周，"
            "通常是材料没有备齐，或者没有人处理。",
            [str(oldest.get("stage_ref") or "")],
        ))
    if ticks.get("available") and ticks["ratio"] >= 0.5:
        idle_lanes = [key for key, value in (ticks.get("idle_by_lane") or {}).items() if value >= 0.9]
        out.append(_candidate(
            f"后台有 {ticks['ratio']:.0%} 的轮次没有工作可做，"
            "是真的没有工作，还是挑不出该做的工作？",
            "研究告一段落后空闲是正常的，但要能和「有工作却挑不出来」区分开，"
            "否则卡住的环节和已完成的环节看起来一样。",
            [f"lane:{name}" for name in idle_lanes[:5]],
        ))
    if scores.get("available") and scores["published"] == 0:
        out.append(_candidate(
            "这一周没有任何研究产出被打质量分，是没有新产出，还是评分还没有接入？",
            "评分工具已经具备，但没有被调用时它不会产生任何结果。",
            [],
        ))
    if journal.get("available") and journal["entries"] == 0:
        out.append(_candidate(
            "这一周没有收到任何反馈，是没有意见，还是不方便反馈？",
            "没有反馈时，无法判断研究产出是否被阅读和认可。",
            [],
        ))
    return out[:MAX_BACKLOG_CANDIDATES]


def policy_suggestions(metrics: Mapping[str, Any]) -> list[str]:
    """Sentences about policy, for the owner.  This module never changes any.

    Engineering notes (attribution, the tick ledger, the scoring hook) are in
    ``technical_notes``: they are for whoever builds the system, not for the
    person deciding the research.
    """

    out: list[str] = []
    spend = metrics["spend"]
    if spend.get("available") and spend.get("share_of_mission_cap") is not None:
        share = spend["share_of_mission_cap"]
        if share < 0.05:
            out.append(
                f"本周实际花费只占本研究任务预算上限的 {_share(share)}，预算不是限制因素；"
                "研究做得少的原因在别处（选题规则、资料、研究安排），"
                "提高预算上限不会让系统多做事。"
            )
    return out[:MAX_POLICY_SUGGESTIONS]


def owner_view(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """The owner's reading of a stored reflection, re-rendered from its metrics.

    A record written before 2026-10-08 carries the developer wording; its
    metrics are the same facts, so the page renders them again with the
    templates above.  ``None`` when the record has no metrics to render.
    """

    metrics = record.get("metrics")
    window = dict(record.get("window") or {})
    window.setdefault("iso_week", record.get("iso_week"))
    if not isinstance(metrics, Mapping) or not window.get("iso_week"):
        return None
    try:
        told = narrative(metrics, window)
        return {
            "title": told["title"], "prose": told["prose"], "table": told["table"],
            "backlog_candidates": backlog_candidates(metrics, window),
            "policy_suggestions": policy_suggestions(metrics),
            "authority_note": OWNER_AUTHORITY_NOTE,
            "technical_notes": technical_notes(metrics),
        }
    except (KeyError, TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# the whole body
# ---------------------------------------------------------------------------


def build_reflection(
    core: sqlite3.Connection,
    *,
    mission: Mapping[str, Any],
    now: datetime | None = None,
    tick_summaries: Sequence[Mapping[str, Any]] = (),
    window: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Everything a reflection says, computed and returned.  Nothing written."""

    moment = now or datetime.now().astimezone()
    frame = dict(window or closed_week(moment))
    metrics = compute_metrics(
        core, window=frame, budget=mission.get("budget"),
        write_scopes=(mission.get("autonomy") or {}).get("may_write") or (),
        tick_summaries=tick_summaries, now=moment,
    )
    mission_ref = _text(mission.get("mission_ref"), "mission.mission_ref")
    return {
        "schema_version": SCHEMA_VERSION,
        "reflection_version": REFLECTION_VERSION,
        "mission_ref": mission_ref,
        "mission_version_ref": _text(mission.get("id"), "mission.id"),
        "iso_week": frame["iso_week"],
        "window": frame,
        "metrics": metrics,
        "narrative": narrative(metrics, frame),
        "backlog_candidates": backlog_candidates(metrics, frame),
        "policy_suggestions": policy_suggestions(metrics),
        "technical_notes": technical_notes(metrics),
        "inputs_hash": inputs_hash(metrics, frame),
        # Said in the record, not only in the docstring: a reader of one row
        # should not have to find this module to learn what it is not allowed
        # to do.
        "authority_note": OWNER_AUTHORITY_NOTE,
    }


def reflection_ref_for(mission_ref: str, iso_week: str) -> str:
    return f"research-cycle-reflection:{mission_ref.split(':', 1)[-1]}:{iso_week}"


# ---------------------------------------------------------------------------
# the authority
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReflectionIdentity:
    """What makes two reflections on one week the same record."""

    reflection_ref: str
    inputs_hash: str


class ResearchCycleReflectionAuthority:
    """Append-only weekly reflections, one version chain per mission-week.

    The only writer in this module, and it writes one table.  Nothing here
    opens a Claim, a deliverable, a mission version or a governance policy --
    which is the freeze, expressed as the absence of an import rather than as a
    promise in a comment.
    """

    _authorized = authorized_flag()

    def __init__(self, store: DaltonStore, *, clock: Callable[[], str] | None = None) -> None:
        self.store = store
        self.connection = store.connection
        self.clock = clock or _now
        self._authorization_flag = authorization_flag(
            self.connection, "dalton_research_cycle_reflection_authorized")
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        if self._authorized:
            raise RuntimeError("ResearchCycleReflectionAuthority operation cannot be nested")
        self._authorized = True
        try:
            with self.store._transaction() as cur:
                yield cur
        finally:
            self._authorized = False

    # -- reads --------------------------------------------------------------

    def latest(self, reflection_ref: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT v.record_json AS record_json, v.content_hash AS content_hash "
            "FROM research_cycle_reflection_pointer p "
            "JOIN research_cycle_reflection_versions v ON v.version_id = p.version_id "
            "WHERE p.reflection_ref = ?",
            (_text(reflection_ref, "reflection_ref"),),
        ).fetchone()
        if row is None:
            return None
        record = json.loads(row["record_json"])
        if record["content_hash"] != row["content_hash"]:
            raise ResearchCycleReflectionConflict("research cycle reflection authority drifted")
        return record

    def versions(self, reflection_ref: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT record_json FROM research_cycle_reflection_versions "
            "WHERE reflection_ref = ? ORDER BY version_number",
            (_text(reflection_ref, "reflection_ref"),),
        ).fetchall()
        return [json.loads(row["record_json"]) for row in rows]

    def for_week(self, mission_ref: str, iso_week: str) -> dict[str, Any] | None:
        return self.latest(reflection_ref_for(mission_ref, iso_week))

    def weeks(self, mission_ref: str) -> list[str]:
        rows = self.connection.execute(
            "SELECT DISTINCT iso_week FROM research_cycle_reflection_versions "
            "WHERE mission_ref = ? ORDER BY iso_week",
            (_text(mission_ref, "mission_ref"),),
        ).fetchall()
        return [row[0] for row in rows]

    # -- write --------------------------------------------------------------

    def record(self, body: Mapping[str, Any], *, actor_ref: str) -> dict[str, Any]:
        """Write one reflection, or return the identical one already written."""

        actor_ref = _text(actor_ref, "actor_ref", maximum=256)
        if not (actor_ref.startswith("automation:") or actor_ref.startswith("human:")):
            raise ResearchCycleReflectionValidationError(
                "actor_ref must be an automation: or human: principal"
            )
        mission_ref = _text(body.get("mission_ref"), "mission_ref")
        mission_version_ref = _text(body.get("mission_version_ref"), "mission_version_ref")
        iso_week = _text(body.get("iso_week"), "iso_week", maximum=16)
        window = body.get("window") or {}
        digest = _text(body.get("inputs_hash"), "inputs_hash", maximum=128)
        if not isinstance(body.get("metrics"), Mapping):
            raise ResearchCycleReflectionValidationError("a reflection needs its metrics")
        candidates = list(body.get("backlog_candidates") or [])
        suggestions = list(body.get("policy_suggestions") or [])
        if len(candidates) > MAX_BACKLOG_CANDIDATES:
            raise ResearchCycleReflectionValidationError(
                f"a reflection may propose at most {MAX_BACKLOG_CANDIDATES} backlog candidates"
            )
        if len(suggestions) > MAX_POLICY_SUGGESTIONS:
            raise ResearchCycleReflectionValidationError(
                f"a reflection may make at most {MAX_POLICY_SUGGESTIONS} policy suggestions"
            )
        for candidate in candidates:
            if not isinstance(candidate, Mapping) or set(candidate) != {"question", "because", "refs"}:
                raise ResearchCycleReflectionValidationError(
                    "a backlog candidate is exactly question, because and refs"
                )
            _text(candidate["question"], "candidate.question", maximum=MAX_QUESTION_CHARS)
            _text(candidate["because"], "candidate.because", maximum=MAX_BECAUSE_CHARS)
        for suggestion in suggestions:
            _text(suggestion, "policy_suggestion", maximum=1000)
        ref = reflection_ref_for(mission_ref, iso_week)
        record = {
            **{key: value for key, value in body.items() if key != "content_hash"},
            "reflection_ref": ref,
            "actor_ref": actor_ref,
            "created_at": self.clock(),
        }
        with self._transaction() as cur:
            seen = cur.execute(
                "SELECT record_json FROM research_cycle_reflection_versions "
                "WHERE reflection_ref = ? AND inputs_hash = ?", (ref, digest),
            ).fetchone()
            if seen is not None:
                return {**json.loads(seen["record_json"]), "status": "duplicate"}
            pointer = cur.execute(
                "SELECT version_id, version_number FROM research_cycle_reflection_pointer "
                "WHERE reflection_ref = ?", (ref,),
            ).fetchone()
            version = 1 if pointer is None else int(pointer["version_number"]) + 1
            prior = None if pointer is None else pointer["version_id"]
            record["version"] = version
            record["prior_version_ref"] = prior
            record["id"] = (
                "research-cycle-reflection-version:"
                + content_hash({"ref": ref, "version": version})[:32]
            )
            record["content_hash"] = content_hash(
                {key: value for key, value in record.items() if key != "content_hash"}
            )
            cur.execute(
                "INSERT INTO research_cycle_reflection_versions(version_id,reflection_ref,"
                "version_number,prior_version_ref,mission_ref,mission_version_ref,iso_week,"
                "window_start,window_end,inputs_hash,backlog_candidate_count,"
                "policy_suggestion_count,record_json,content_hash,actor_ref,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (record["id"], ref, version, prior, mission_ref, mission_version_ref, iso_week,
                 str(window.get("start") or ""), str(window.get("end") or ""), digest,
                 len(candidates), len(suggestions),
                 json.dumps(record, ensure_ascii=False, sort_keys=True), record["content_hash"],
                 actor_ref, record["created_at"]),
            )
            if pointer is None:
                cur.execute(
                    "INSERT INTO research_cycle_reflection_pointer(reflection_ref,version_id,"
                    "version_number,content_hash,updated_at) VALUES(?,?,?,?,?)",
                    (ref, record["id"], version, record["content_hash"], record["created_at"]),
                )
            else:
                cur.execute(
                    "UPDATE research_cycle_reflection_pointer SET version_id=?, version_number=?, "
                    "content_hash=?, updated_at=? WHERE reflection_ref=?",
                    (record["id"], version, record["content_hash"], record["created_at"], ref),
                )
        written = self.latest(ref)
        if written is None or written["id"] != record["id"]:
            raise ResearchCycleReflectionConflict("the reflection did not read back")
        return {**written, "status": "fresh"}


__all__ = [
    "IDLE_LANE_STATUSES",
    "RESEARCH_TASK_SCOPE",
    "MAX_BACKLOG_CANDIDATES",
    "MAX_POLICY_SUGGESTIONS",
    "NARRATIVE_TITLE",
    "REFLECTION_VERSION",
    "SCHEMA_VERSION",
    "WORK_ORDER_FAMILY_LANES",
    "WRITE_SCOPE",
    "ReflectionIdentity",
    "ResearchCycleReflectionAuthority",
    "ResearchCycleReflectionConflict",
    "ResearchCycleReflectionError",
    "ResearchCycleReflectionValidationError",
    "backlog_candidates",
    "backlog_movement",
    "build_reflection",
    "claims_retired",
    "closed_week",
    "compute_metrics",
    "idle_tick_ratio",
    "inputs_hash",
    "iso_week_label",
    "journal_feedback",
    "judgement_outcomes",
    "narrative",
    "open_human_checkpoints",
    "owner_view",
    "pool_label",
    "technical_notes",
    "planner_inquiries",
    "policy_suggestions",
    "quality_scores_published",
    "reflection_ref_for",
    "spend_by_pool",
    "work_order_family",
]
