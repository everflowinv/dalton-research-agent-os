"""Why a company's Initial Screen is waiting, and who has to move.

2026-10-02 (ws-7d): all four hyperscalers sat in ``initial_screen entered`` for
fifteen days.  Every hour the drafting lane ran, found every company short of
its data foundation, and reported ``idle: nothing to draft`` -- the same word a
finished queue uses.  Nothing downstream had anything to do, and nothing told
the owner, because "idle" is a healthy word on every page that reads it.

The checklist (``mission_stage.evaluate_mission``) already knew *what* was
missing.  What it did not say is *why it is not arriving*, and those are very
different stalls:

* the source found it and the pipeline has not fetched it yet (it will, by
  itself, once the queue moves);
* the source was searched and never returned it (a search plan problem -- a
  person has to change the plan);
* the fetch keeps failing (a code or connector problem);
* nothing was ever searched for it (not planned, not connected).

This module turns one checklist row into one of those, in the owner's words,
with what to do.  Pure: it reads the rows it is handed and two small queries
over the mission's own tables; it writes nothing.  The drafting lane puts its
answer in ``summary.json``; the owner's to-do list and the parity check read it
from there, so nobody re-runs the (slow) checklist to find out.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "0.1"
#: Reason code on the drafting lane's summary and tick when every candidate
#: company is waiting on its data foundation.
REASON_CODE = "data_foundation_incomplete"
#: After this long the wait is the owner's business, not a warm-up.  A fresh
#: mission takes a few days to search and fetch; two weeks is a stall.
ESCALATE_AFTER = timedelta(days=3)
#: Where the drafting lane keeps its tickets (``initial_screen_launcher``).
TICKETS_DIRNAME = "initial-screens"

# Causes, closed.  ``automatic`` says whether the system will close the gap
# without anybody acting, which is what decides whether the owner is asked.
CAUSES: Mapping[str, dict[str, Any]] = {
    "found_waiting_acquisition": {"automatic": True, "label": "已经找到，等待取回"},
    "not_returned_by_search": {"automatic": False, "label": "检索没有找到"},
    "fetch_failed": {"automatic": False, "label": "取回一直失败"},
    "fetch_too_large": {"automatic": False, "label": "原文超过抓取大小上限"},
    "queued": {"automatic": True, "label": "已在取回队列"},
    "not_planned": {"automatic": False, "label": "没有对应的检索计划"},
    "source_unavailable": {"automatic": False, "label": "来源没有接入"},
    "figures_missing": {"automatic": True, "label": "财报数字还没入账"},
}


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def waiting_since(connection: sqlite3.Connection, mission_ref: str) -> dict[str, str]:
    """company_ref → when its Initial Screen last changed state, across versions."""

    try:
        rows = connection.execute(
            "SELECT company_ref, MAX(created_at) AS at FROM coverage_mission_stage_records "
            "WHERE stage_ref='initial_screen' AND mission_version_ref IN ("
            "SELECT mission_version_id FROM coverage_mission_versions WHERE mission_ref=?) "
            "GROUP BY company_ref", (mission_ref,),
        ).fetchall()
    except sqlite3.Error:
        return {}
    return {row[0]: row[1] for row in rows if row[1]}


def latest_failures(connection: sqlite3.Connection, mission_ref: str) -> dict[tuple[str, str], str]:
    """(company_ref, spec_ref) → the newest acquisition failure reason."""

    try:
        rows = connection.execute(
            "SELECT d.company_ref AS company_ref, s.spec_ref AS spec_ref, "
            "d.failure_reason AS reason, d.updated_at AS at "
            "FROM coverage_mission_discovered_documents d "
            "JOIN coverage_mission_source_discoveries s ON s.record_id=d.discovery_ref "
            "WHERE d.status='acquisition_failed' AND d.failure_reason IS NOT NULL "
            "AND d.mission_version_ref IN (SELECT mission_version_id "
            "FROM coverage_mission_versions WHERE mission_ref=?) "
            "ORDER BY d.updated_at", (mission_ref,),
        ).fetchall()
    except sqlite3.Error:
        return {}
    out: dict[tuple[str, str], str] = {}
    for row in rows:
        out[(row[0], row[1])] = str(row[2])
    return out


def _periods(values: Sequence[str]) -> str:
    return "、".join(values)


def diagnose_item(
    item: Mapping[str, Any], *, ticker: str,
    failure_reason: str | None = None,
) -> dict[str, Any]:
    """One missing checklist item → cause, owner sentence and action."""

    status = item.get("status")
    have, required = int(item.get("have") or 0), int(item.get("required") or 0)
    candidates = int(item.get("candidate_count") or 0)
    pending = int(item.get("pending") or 0)
    failed = int(item.get("failed") or 0)
    item_ref = item.get("item_ref")
    short = f"还差 {max(required - have, 0)} 份（已有 {have}/{required}）"
    if status == "source_unavailable":
        cause = "source_unavailable"
        why = f"{item.get('source_ref')} 没有接入，这一项拿不到"
        action = f"在「来源」页接入 {item.get('source_ref')}，或决定这一项不再要求"
    elif status == "not_planned":
        cause = "not_planned"
        why = item.get("note") or "没有对应的检索计划"
        action = "给这一项加一条检索计划（discovery plan），或决定这一项不再要求"
    elif item_ref == "quarterly_financials":
        cause = "figures_missing"
        why = f"SEC 财报数字{short}"
        action = "不用你处理：系统会自动补齐 SEC 季度数字"
    elif item_ref == "earnings_calls":
        missing = list(item.get("missing_periods") or ())
        found = list(item.get("candidate_periods") or ())
        wanted = missing or None
        found_wanted = [period for period in found if wanted is None or period in wanted]
        if found_wanted and (wanted is None or set(found_wanted) >= set(wanted)) and (
                wanted is not None or len(found_wanted) >= required - have):
            cause = "found_waiting_acquisition"
            why = (f"电话会纪要{short}；缺的季度（{_periods(found_wanted)}）AlphaEngine 已经检索到，"
                   "还在等候选选取和取回")
            action = ("不用你处理：AlphaEngine 候选选取轮到这家公司后会自动取回。"
                      "若这一条超过几天不动，看「运行状态」里 AlphaEngine 的选取队列是否卡住")
        else:
            still = [period for period in (wanted or ()) if period not in found_wanted]
            cause = "not_returned_by_search"
            quarters = (f"{_periods(still)} 的" if still else "最近 4 个季度的")
            partial = (f"（{_periods(found_wanted)} 已找到，等待取回）" if found_wanted else "")
            why = (f"电话会纪要{short}；AlphaEngine 检索一直没有返回 {ticker} {quarters}纪要"
                   f"{partial}；检索词只有股票代码，结果里都是其他公司或其他场合的会议，重复同样的检索也不会变")
            action = (f"更新 AlphaEngine 检索计划：用公司全名和别名（不只是代码 {ticker}）"
                      "检索电话会纪要，并批准新计划；或决定这家公司的初筛先不要求纪要")
    elif failed and not have:
        too_large = "ResponseTooLarge" in str(failure_reason or "")
        cause = "fetch_too_large" if too_large else "fetch_failed"
        reason = str(failure_reason or "未知原因")[:200]
        why = f"{item.get('label')}{short}；已找到 {failed} 份，但取回一直失败：{reason}"
        action = (("这是代码里的抓取大小上限（不是研究判断）：部署放宽 SEC 原文大小上限的版本后，"
                   "失败冷却结束会自动重取")
                  if too_large else
                  "确认这份原文是否还能取到；需要的话换一个来源或人工取回")
    elif pending:
        cause = "queued"
        why = f"{item.get('label')}{short}；{pending} 份已在取回队列"
        action = "不用你处理：系统会自动完成取回"
    elif candidates:
        cause = "found_waiting_acquisition"
        why = f"{item.get('label')}{short}；已发现 {candidates} 份候选，还没有被选中取回"
        action = ("不用你处理：候选选取轮到这家公司后会自动取回。"
                  "若超过几天不动，看「运行状态」里 AlphaEngine 的选取队列是否卡住")
    else:
        cause = "not_returned_by_search"
        why = f"{item.get('label')}{short}；检索没有找到任何候选"
        action = "检查这一项的检索计划（公司名称、别名、文档类型）"
    return {
        "item_ref": item_ref, "label": item.get("label"), "have": have, "required": required,
        "status": status, "cause": cause, "cause_label": CAUSES[cause]["label"],
        "automatic": bool(CAUSES[cause]["automatic"]), "why": why, "action": action,
        **({"missing_periods": list(item.get("missing_periods") or ()),
            "candidate_periods": list(item.get("candidate_periods") or ())}
           if item_ref == "earnings_calls" else {}),
    }


def foundation_blockers(
    stage_rows: Sequence[Mapping[str, Any]], *,
    waiting: Mapping[str, str] | None = None,
    failures: Mapping[tuple[str, str], str] | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Every company in ``initial_screen`` whose data foundation blocks drafting."""

    moment = now or datetime.now(timezone.utc)
    out: list[dict[str, Any]] = []
    for row in stage_rows:
        if row.get("stage") != "initial_screen" or row.get("stage_status") == "gate_passed":
            continue
        blocking = [item for item in row.get("items") or ()
                    if item.get("status") in {"partial", "missing"}]
        if not blocking:
            continue
        company_ref = row["company_ref"]
        ticker = str(row.get("ticker") or company_ref.rsplit(":", 1)[-1].upper())
        items = []
        for item in blocking:
            reason = None
            for spec in item.get("spec_refs") or ():
                reason = (failures or {}).get((company_ref, spec)) or reason
            items.append(diagnose_item(item, ticker=ticker, failure_reason=reason))
        since = (waiting or {}).get(company_ref)
        started = _parse(since)
        days = None if started is None else max(0, (moment - started).days)
        out.append({
            "company_ref": company_ref, "ticker": ticker, "priority": row.get("priority"),
            "waiting_since": since, "days_waiting": days,
            "missing": [item["label"] for item in items],
            "needs_owner": any(not item["automatic"] for item in items),
            "items": items,
        })
    return out


def hold_sentence(blockers: Sequence[Mapping[str, Any]]) -> str:
    """One line for the lane's tick: who is waiting on what, and for how long."""

    parts = []
    for block in blockers:
        days = block.get("days_waiting")
        waited = f"（已等 {days} 天）" if isinstance(days, int) else ""
        causes = "；".join(f"{item['label']}：{item['cause_label']}" for item in block["items"])
        parts.append(f"{block['ticker']}{waited} {causes}")
    return (f"资料底座还没齐，{len(blockers)} 家公司的初筛都在等："
            + " | ".join(parts))[:1500]


def latest_summary(state_dir: Path | str | None) -> dict[str, Any] | None:
    """The drafting lane's newest finished summary, or ``None``.

    ``latest.json`` names the newest ticket; while that one is still running
    its predecessor's summary is the newest answer, so fall back to the newest
    ``summary.json`` on disk.  Never raises: this is read by display code.
    """

    if state_dir is None:
        return None
    root = Path(state_dir).expanduser() / TICKETS_DIRNAME
    candidates: list[Path] = []
    try:
        latest = json.loads((root / "latest.json").read_text(encoding="utf-8"))
        ref = str((latest or {}).get("ticket_ref") or "")
        if ":" in ref:
            candidates.append(root / ref.split(":", 1)[1] / "summary.json")
    except (OSError, ValueError, TypeError):
        pass
    try:
        others = sorted(root.glob("*/summary.json"), key=lambda path: path.stat().st_mtime,
                        reverse=True)[:5]
    except OSError:
        others = []
    candidates.extend(others)
    for path in candidates:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(value, dict):
            return value
    return None


def overdue(summary: Mapping[str, Any] | None, *, now: datetime,
            after: timedelta = ESCALATE_AFTER) -> list[dict[str, Any]]:
    """Blocked companies from a lane summary that have waited past ``after``."""

    if not isinstance(summary, Mapping) or summary.get("reason_code") != REASON_CODE:
        return []
    foundation = summary.get("foundation") or {}
    out = []
    for block in foundation.get("companies") or ():
        started = _parse(block.get("waiting_since"))
        if started is None or now - started < after:
            continue
        out.append({**block, "days_waiting": (now - started).days})
    return out


__all__ = [
    "CAUSES", "ESCALATE_AFTER", "REASON_CODE", "SCHEMA_VERSION", "TICKETS_DIRNAME",
    "diagnose_item", "foundation_blockers", "hold_sentence", "latest_failures",
    "latest_summary", "overdue", "waiting_since",
]
