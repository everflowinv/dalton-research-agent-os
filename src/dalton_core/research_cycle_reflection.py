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

from .store import DaltonStore, content_hash

SCHEMA_VERSION = "0.1"
_SCHEMA_PATH = Path(__file__).with_name("research_cycle_reflection_schema.sql")

# Bumped when a metric changes what it counts.  Part of ``inputs_hash``, so a
# fixed counter re-reflects a week that a broken one already reflected on.
REFLECTION_VERSION = "0.1"

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
    retired = [row for row in decisions
               if row["decision"] == "retired" and _in_window(row["created_at"], window)]
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
            "research_quality_score_versions 不在这个 Core 里：Q1 的质量分 authority 还没有部署"
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

    table: list[dict[str, Any]] = []
    if spend.get("available"):
        for pool in spend["pools"]:
            table.append({
                "pool": pool["pool"],
                "lane": pool["lane"] or "—",
                "cost_usd": pool["cost_usd"],
                "calls": pool["calls"],
                "share_of_spend": pool["share_of_spend"],
                "share_of_mission_cap": pool["share_of_mission_cap"],
            })

    lines: list[str] = []
    if spend.get("available"):
        if spend["calls"] == 0:
            lines.append(
                f"这一周（{window['iso_week']}）没有任何计价的模型调用。"
                "系统开着，但没有花钱，也就没有把时间花在任何一条 lane 上。"
            )
        else:
            top = spend["pools"][0]
            share = spend.get("share_of_mission_cap")
            cap_clause = (
                "" if share is None else
                f"，占 mission 这一周预算上限（{_usd(spend['mission_window_cap_usd'])}）的{_share(share)}"
            )
            lines.append(
                f"这一周（{window['iso_week']}）一共花了 {_usd(spend['total_cost_usd'])}、"
                f"{spend['calls']} 次计价调用{cap_clause}。"
                f"最大的一池是 {top['pool']}"
                + (f"（{top['lane']} lane）" if top["lane"] else "（没有已注册 lane 认领它）")
                + f"，{_usd(top['cost_usd'])}，占本周花费的 {top['share_of_spend']:.0%}。"
            )
            if spend["pool_count"] == 1:
                lines.append(
                    "只有一池在花钱：这一周系统只做了一件事，"
                    "无论它同时开着几条 lane。"
                )
    else:
        lines.append(f"花费不可归集：{spend.get('reason')}")

    if backlog.get("available"):
        lines.append(
            f"问题账本上，本周新登记 {backlog['new_questions']} 条、被回答 {backlog['answered']} 条，"
            f"周末仍未关闭的有 {backlog['open_at_end']} 条。"
            + ("新登记多于被回答，未决问题集合在这一周变大了。"
               if backlog["new_questions"] > backlog["answered"] else
               ("被回答的不少于新登记的，未决问题集合没有变大。"
                if backlog["answered"] else "既没有新问题，也没有问题被回答。"))
        )
    if claims.get("available"):
        lines.append(
            f"Claim 层退役 {claims['retired']} 条、保留 {claims['kept']} 条，"
            f"本周新提出 {claims['challenges_raised']} 条挑战，"
            f"周末还有 {claims['challenges_open_at_end']} 条挑战没有裁决。"
        )
    if planner.get("available"):
        ratio = planner["dispatch_ratio"]
        lines.append(
            f"规划器本周记录 {planner['plans_recorded']} 份 research plan、"
            f"提出 {planner['inquiries_raised']} 条 inquiry，其中 {planner['dispatched']} 条被派发"
            + ("。" if ratio is None else f"（{ratio:.0%}）。")
            + (f"{planner['dispatch_reason']}" if planner.get("dispatch_reason") else "")
        )
    if ticks.get("available"):
        lines.append(
            f"{ticks['ticks']} 次 tick 里有 {ticks['idle_ticks']} 次每条 lane 都报了 idle 或 skipped，"
            f"闲置率 {ticks['ratio']:.0%}。"
        )
    else:
        lines.append(f"闲置率不可算：{ticks.get('reason')}")
    if checkpoints.get("available") and checkpoints["open"]:
        lines.append(
            f"有 {checkpoints['open']} 个 stage 停在 entered 等人，"
            f"最老的一个已经等了 {checkpoints['oldest_age_days']} 天；"
            f"本周关掉了 {checkpoints['closed_this_week']} 个。"
        )
    if scores.get("available"):
        lines.append(
            f"本周发布 {scores['published']} 条质量分（{scores['judged']} 条带判读，"
            f"{scores['deterministic_only']} 条只有确定性层），覆盖 {scores['distinct_targets']} 份产出。"
        )
    else:
        lines.append(f"质量分不可读：{scores.get('reason')}")
    if not journal.get("available"):
        lines.append(f"人类反馈不可读：{journal.get('reason')}")
    elif journal["entries"]:
        words = "、".join(
            f"{word} {count}" for word, count in journal["by_word"].items() if count
        )
        partial = ("（只数了两处反馈表里的一处，"
                   + "、".join(journal["missing_sources"]) + " 不在这个 Core 里）"
                   if journal.get("partial") else "")
        lines.append(f"人给了 {journal['entries']} 条反馈：{words}{partial}。")
    else:
        partial = ("；另一处（" + "、".join(journal["missing_sources"])
                   + "）不在这个 Core 里，没有数进来"
                   if journal.get("partial") else "")
        lines.append(
            f"这一周没有人给过任何一条反馈{partial}。"
            "五个词的反馈词表还没有 UI，所以「没有反馈」暂时不能读成「读过而没有意见」。"
        )
    return {"title": NARRATIVE_TITLE, "prose": "\n".join(lines), "table": table[:MAX_TABLE_ROWS]}


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
            f"{window['iso_week']} 花了 {_usd(spend['total_cost_usd'])} 却没有登记任何一条新问题，"
            f"最大的一池 {top['pool']} 到底买到了什么？",
            "一周的花费如果没有让未决问题集合变化，那它要么在补已知的证据，要么在重复。"
            "两种都可能是对的，但要说得出是哪一种。",
            [f"pool:{pool['pool']}" for pool in spend["pools"][:3]],
        ))
    if (planner.get("available") and planner["inquiries_raised"] > 0
            and planner["dispatched"] == 0):
        out.append(_candidate(
            f"规划器这一周提出了 {planner['inquiries_raised']} 条 inquiry 而一条都没有被派发，"
            "其中哪几条值得先做成 ResearchTask？",
            planner.get("dispatch_reason")
            or "inquiry 只有被派发才会变成证据；没有派发路径时它们只是记录。",
            [],
        ))
    if backlog.get("available") and backlog["new_questions"] > 3 * max(backlog["answered"], 1):
        out.append(_candidate(
            f"未决问题这一周净增 {backlog['new_questions'] - backlog['answered']} 条，"
            "哪一条最该先被回答？",
            "问题登记得比回答快三倍以上时，账本在变成愿望清单。",
            backlog["new_question_refs"][:5],
        ))
    if checkpoints.get("available") and (checkpoints["oldest_age_days"] or 0) >= 14:
        oldest = checkpoints["checkpoints"][0] if checkpoints["checkpoints"] else {}
        out.append(_candidate(
            f"{oldest.get('stage_ref', '某个 stage')}（{oldest.get('company_ref', '')}）"
            f"已经在 entered 上等了 {checkpoints['oldest_age_days']} 天，它在等什么？",
            "人类检查点是设计里的停顿，不是设计里的死锁。等待超过两周说明要么材料没备齐，"
            "要么这个检查点没人认领。",
            [str(oldest.get("stage_ref") or "")],
        ))
    if ticks.get("available") and ticks["ratio"] >= 0.5:
        idle_lanes = [key for key, value in (ticks.get("idle_by_lane") or {}).items() if value >= 0.9]
        out.append(_candidate(
            f"一半以上的 tick（{ticks['ratio']:.0%}）里每条 lane 都无事可做，"
            "这些 lane 是真的没有工作，还是选择规则挑不出来？",
            "闲置本身不是错——五家公司做完规格之后就该安静。但闲置率要能和「有工作但挑不出来」"
            "分开，否则一条卡住的 lane 和一条完成的 lane 长得一样。",
            [f"lane:{name}" for name in idle_lanes[:5]],
        ))
    if scores.get("available") and scores["published"] == 0:
        out.append(_candidate(
            "这一周没有为任何一份产出打过质量分，是没有新产出，还是打分没有接进任何一条 lane？",
            "Q1 建了评分表与打分器但没有建 lane：没有人调用它，它就只是一个能力。",
            [],
        ))
    if journal.get("available") and journal["entries"] == 0:
        # Fires even when the count is partial: zero on the table that *is*
        # installed is still zero, and the missing half is named in the
        # ``because`` rather than used as a reason to stay quiet.
        partial = (
            "（这一周只数得到两处反馈表里的一处，"
            + "、".join(journal.get("missing_sources") or []) + " 还没有部署）"
            if journal.get("partial") else ""
        )
        out.append(_candidate(
            "这一周没有任何人类反馈落到 AnalystJournal 或周报反馈上，缺的是意见还是入口？",
            "五个词的反馈词表已经存在两处，两处都还没有 UI。没有入口时的沉默读不出任何东西。"
            + partial,
            [],
        ))
    return out[:MAX_BACKLOG_CANDIDATES]


def policy_suggestions(metrics: Mapping[str, Any]) -> list[str]:
    """Sentences about policy.  This module never changes any."""

    out: list[str] = []
    spend = metrics["spend"]
    ticks = metrics["ticks"]
    scores = metrics["quality_scores"]
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
    if spend.get("available") and spend.get("share_of_mission_cap") is not None:
        share = spend["share_of_mission_cap"]
        if share < 0.05:
            out.append(
                f"本周实际花费只占 mission 预算上限的{_share(share)}。"
                "预算不是瓶颈，所以「做得少」的原因要到别处找（选择规则、素材、派发路径），"
                "提高上限不会让系统做更多事。"
            )
    return out[:MAX_POLICY_SUGGESTIONS]


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
        "inputs_hash": inputs_hash(metrics, frame),
        # Said in the record, not only in the docstring: a reader of one row
        # should not have to find this module to learn what it is not allowed
        # to do.
        "authority_note": (
            "这条记录只读不写：它不写 Ledger、不改 policy、不登记问题。"
            "backlog_candidates 是候选，要由 planner 或人经 ResearchQuestionBacklog 的准入路径登记；"
            "policy_suggestions 是句子。"
        ),
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

    def __init__(self, store: DaltonStore, *, clock: Callable[[], str] | None = None) -> None:
        self.store = store
        self.connection = store.connection
        self.clock = clock or _now
        self._authorized = False
        self.connection.create_function(
            "dalton_research_cycle_reflection_authorized", 0, lambda: int(self._authorized)
        )
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
    "narrative",
    "open_human_checkpoints",
    "planner_inquiries",
    "policy_suggestions",
    "quality_scores_published",
    "reflection_ref_for",
    "spend_by_pool",
    "work_order_family",
]
