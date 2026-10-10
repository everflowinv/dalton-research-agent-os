"""The cockpit's weekly brief page: what is scheduled, what was delivered.

Kept out of ``cockpit_plane`` on purpose: this is one small owner decision
(``publish_workspace_weekly_brief_plan``) and one read, and the plane is a
large module other work changes often.  Both functions take the plane so the
write goes out through exactly the owner-principal path every other cockpit
decision uses (``CockpitPlane._governance``), and the read uses the plane's
own configured paths.

The read is read-only: the plan file, the local briefs directory, and a
``mode=ro`` connection to Core for the last admissions and deliveries.
"""

from __future__ import annotations

import sqlite3
import re
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .store import content_hash
from .weekly_brief_workspace_plan import (
    WorkspaceWeeklyBriefError,
    is_local_destination,
    local_briefs,
    read_plan,
)

WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
_FIELDS = {"weekday", "hour", "minute", "timezone", "destination_ref", "expected_plan_hash"}


def _recent(core_db: Path, limit: int = 8) -> dict[str, list[dict[str, Any]]]:
    try:
        connection = sqlite3.connect(f"file:{core_db}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return {"cycles": [], "deliveries": []}
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        cycles, deliveries = [], []
        for table, progress in (("weekly_brief_cycle_admissions", False),
                                ("weekly_brief_progress_cycle_admissions", True)):
            if table not in tables:
                continue
            rows = [dict(row) for row in connection.execute(
                "SELECT cycle_id, plan_ref, scheduled_for, issue_version_ref, destination_ref "
                f"FROM {table} ORDER BY scheduled_for DESC LIMIT ?", (limit,))]
            if progress:
                for row in rows:
                    row.update(brief_kind="mission_progress", brief_kind_label="研究进展周报")
            cycles.extend(rows)
        for table, progress in (("weekly_brief_deliveries", False),
                                ("weekly_brief_progress_deliveries", True)):
            if table not in tables:
                continue
            rows = [dict(row) for row in connection.execute(
                "SELECT issue_version_ref, destination_ref, external_message_ref, "
                f"delivered_at FROM {table} "
                "ORDER BY delivered_at DESC LIMIT ?", (limit,))]
            if progress:
                for row in rows:
                    row.update(brief_kind="mission_progress", brief_kind_label="研究进展周报")
            deliveries.extend(rows)
        if "weekly_brief_issue_versions" in tables:
            admission_filter = (" WHERE NOT EXISTS (SELECT 1 FROM weekly_brief_cycle_admissions a "
                                "WHERE a.issue_version_ref=i.version_id)"
                                if "weekly_brief_cycle_admissions" in tables else "")
            destination = ("(SELECT d.destination_ref FROM weekly_brief_deliveries d "
                           "WHERE d.issue_version_ref=i.version_id ORDER BY d.delivered_at DESC LIMIT 1)"
                           if "weekly_brief_deliveries" in tables else "NULL")
            rows = [dict(row) for row in connection.execute(
                "SELECT NULL AS cycle_id, NULL AS plan_ref, i.period_end AS scheduled_for, "
                f"i.version_id AS issue_version_ref, {destination} AS destination_ref "
                f"FROM weekly_brief_issue_versions i{admission_filter} "
                "ORDER BY i.period_end DESC LIMIT ?", (limit,))]
            for row in rows:
                row["manual"] = True
            cycles.extend(rows)
        cycles.sort(key=lambda row: (row["scheduled_for"], row["cycle_id"] or row["issue_version_ref"]), reverse=True)
        deliveries.sort(key=lambda row: (row["delivered_at"], row["issue_version_ref"]), reverse=True)
        cycles, deliveries = cycles[:limit], deliveries[:limit]
    except sqlite3.Error:
        cycles, deliveries = [], []
    finally:
        connection.close()
    return {"cycles": cycles, "deliveries": deliveries}


def next_run_at(plan: Mapping[str, Any] | None, now: datetime | None = None) -> str | None:
    """The next scheduled time of a plan, in UTC ISO form (2026-10-09, W03/W04).

    Computed in the plan's own time zone, so a daylight-saving change moves it
    the way the schedule does.  ``None`` for no plan or an unknown zone.
    """

    if not isinstance(plan, Mapping):
        return None
    try:
        zone = ZoneInfo(str(plan["timezone"]))
        weekday, hour, minute = int(plan["weekday"]), int(plan["hour"]), int(plan["minute"])
    except (KeyError, TypeError, ValueError, ZoneInfoNotFoundError):
        return None
    local_now = (now or datetime.now(timezone.utc)).astimezone(zone)
    for days in range(0, 8):
        day = (local_now + timedelta(days=days)).date()
        if day.weekday() != weekday:
            continue
        candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)
        if candidate > local_now:
            return candidate.astimezone(timezone.utc).isoformat(timespec="seconds")
    return None


def _destinations(plan: Mapping[str, Any] | None, recent: Mapping[str, list[dict[str, Any]]]) -> list[str]:
    """Every destination this project has used, current first (W02: choose, not type)."""

    seen: list[str] = []
    for ref in ([plan.get("destination_ref")] if isinstance(plan, Mapping) else []) + [
            row.get("destination_ref") for key in ("cycles", "deliveries") for row in recent.get(key) or ()]:
        if isinstance(ref, str) and ref and ref not in seen:
            seen.append(ref)
    return seen


def weekly_brief_view(plane: Any) -> dict[str, Any]:
    config = plane.config
    state = Path(config.state_dir)
    try:
        plan = read_plan(state)
        problem = None
    except WorkspaceWeeklyBriefError as exc:
        plan, problem = None, str(exc)
    summary = None
    if plan is not None:
        summary = {
            "plan_ref": plan["plan_ref"], "brief_ref": plan["brief_ref"],
            "weekday": plan["weekday"], "weekday_label": WEEKDAYS[plan["weekday"]],
            "hour": plan["hour"], "minute": plan["minute"], "timezone": plan["timezone"],
            "destination_ref": plan["destination_ref"],
            "local": is_local_destination(plan["destination_ref"]),
            "effective_from": plan["effective_from"],
            "evidence_pack_ref": (plan.get("evidence_refresh") or {}).get("evidence_pack_ref"),
        }
    recent = _recent(Path(config.core_db))
    return {
        "enabled": True,
        "plan": summary,
        "plan_hash": None if plan is None else content_hash(plan),
        "problem": problem,
        "local_briefs": local_briefs(state),
        "next_run_at": next_run_at(summary),
        "destinations": _destinations(summary, recent),
        **recent,
    }


def weekly_brief_document(plane: Any, query: Mapping[str, str]) -> dict[str, Any]:
    """Read an exact formal issue; no filename, renderer write or writer call."""
    from .cockpit_plane import CockpitError
    from .coverage_mission import CoverageMissionAuthority
    from .industry_research import IndustryResearchAuthority
    from .mission_progress_weekly import MissionProgressWeeklyAuthority
    from .weekly_brief import WeeklyBriefAuthority

    ref = query.get("issue_version_ref")
    if set(query) != {"issue_version_ref"} or not isinstance(ref, str) or len(ref) > 512 \
            or not re.fullmatch(r"weekly-brief(?:-progress)?-version:[\w:.-]+", ref) \
            or ".." in ref:
        raise CockpitError("请选择已生成的周报")
    connection = None
    try:
        core_db = Path(plane.config.core_db)
        connection = sqlite3.connect(core_db.as_uri() + "?mode=ro", uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        mission_reader = object.__new__(CoverageMissionAuthority)
        mission_reader.connection = connection
        mission_ref = getattr(plane.config, "mission_ref", None)
        if mission_ref is None:
            pointer = connection.execute(
                "SELECT mission_ref FROM coverage_mission_pointer ORDER BY updated_at DESC LIMIT 1").fetchone()
            if pointer is None:
                raise CockpitError("尚未发布研究目标，暂不能查看周报")
            mission_ref = pointer["mission_ref"]
        mission = mission_reader._active_mission_record(mission_ref)
        industry = object.__new__(IndustryResearchAuthority)
        industry.connection = connection
        weekly = object.__new__(WeeklyBriefAuthority)
        weekly.connection = connection
        weekly.store = SimpleNamespace(path=str(core_db), connection=connection)
        weekly.industry_research = industry
        weekly.progress = MissionProgressWeeklyAuthority(weekly)
        issue = weekly.issue(ref)
        if issue.get("brief_kind") == "mission_progress":
            in_scope = issue["snapshot"]["mission_ref"] == mission["mission_ref"]
        else:
            # Older research issues do not bind a mission. Match their exact
            # company/industry scope to an actual version of this mission,
            # including its history after a coverage change.
            companies = set(issue["content_index"]["company_refs"])
            in_scope = False
            for row in connection.execute(
                    "SELECT mission_version_id FROM coverage_mission_versions WHERE mission_ref=?",
                    (mission["mission_ref"],)):
                historic = mission_reader._mission_record(row["mission_version_id"])
                if historic["industry_ref"] == issue["industry_ref"] and companies <= {
                        member["company_ref"] for member in historic["universe"]}:
                    in_scope = True
                    break
        if not in_scope:
            raise CockpitError("这份周报不属于当前研究项目")
        rendered = weekly.render_markdown(ref)
        return {
            "issue_version_ref": issue["id"], "issue_version_hash": issue["content_hash"],
            "brief_kind": issue.get("brief_kind", "research"),
            "title": "研究进展周报" if issue.get("brief_kind") == "mission_progress" else "投研周报",
            "period": issue["period"], "body": rendered["body"], "media_type": "text/markdown",
        }
    except CockpitError:
        raise
    except (ValueError, RuntimeError, OSError, sqlite3.Error, KeyError) as exc:
        raise CockpitError("周报暂时读不出来：来源或版本需要核对，请联系管理员") from exc
    finally:
        if connection is not None:
            connection.close()


def publish_weekly_brief_plan(plane: Any, login: str, value: Mapping[str, Any]) -> dict[str, Any]:
    from .cockpit_plane import CockpitError

    if not isinstance(value, Mapping) or set(value) - _FIELDS \
            or not {"weekday", "hour", "minute", "timezone"} <= set(value):
        raise CockpitError("请填写每周的星期、时间和时区")
    params = {key: value.get(key) for key in _FIELDS}
    if isinstance(params["destination_ref"], str) and not params["destination_ref"].strip():
        params["destination_ref"] = None
    return plane._governance(login, "publish_workspace_weekly_brief_plan", params,
                             failure="每周简报计划没有保存")


__all__ = ["next_run_at", "publish_weekly_brief_plan", "weekly_brief_view", "weekly_brief_document"]
