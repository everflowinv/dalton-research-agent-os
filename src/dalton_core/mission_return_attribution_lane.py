"""ADR-0014: keep the income bridges, per-share series and return bridges current.

In process, no model, no network, no child -- like the scenario observation
lane.  What needs doing is derived every tick from one hashed ``MAX(rowid)``
signature over the input tables: statement filings and lines, the driver
model, the valuation snapshot (which itself moves with every settled close),
the consensus, the debate maps, the conviction calls and the mission.  When
the signature moves (and at least :data:`MIN_SCAN_SECONDS` after the last
scan), or :data:`MAX_IDLE_SECONDS` have passed regardless, every company of
the mission is marked due; each tick then refreshes **one** company, so the
writer thread never spends more than one company's arithmetic per tick.  An
object is appended only when its body changed, so a quiet day writes nothing.

It runs at order 98: after the valuation snapshot (97) it reads, before the
quantitative-claim lane (99).  It writes under the ``valuation`` grant -- the
same authority family as the snapshot it extends -- and a mission without
that grant gets ``ungranted`` with a Chinese reason, every tick.  A new
workspace needs nothing: the tables appear the first time a company is
refreshed, and the mission a created workspace publishes grants every
automation scope.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from .lane_registry import LaneSpec, register_lane

DRIVER_KEY = "return_attribution"
STATE_KEY = "return_attribution_state"
WRITE_SCOPE = "valuation"
MIN_SCAN_SECONDS = 300
MAX_IDLE_SECONDS = 6 * 3600
MAX_DETAIL = 500
INPUT_TABLES = (
    "coverage_mission_statement_filings", "coverage_mission_statement_lines",
    "forecast_model_versions", "valuation_snapshot_versions", "consensus_estimate_versions",
    "debate_map_versions", "conviction_call_proposals", "coverage_mission_versions",
)


def input_signature(connection: Any, tables: tuple[str, ...] = INPUT_TABLES) -> str:
    parts = []
    for table in tables:
        try:
            value = connection.execute(f"SELECT MAX(rowid) FROM {table}").fetchone()[0]
        except Exception:  # noqa: BLE001 - a missing table is a state, not an error
            value = "missing"
        parts.append(f"{table}:{value}")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _current_mission(server: Any) -> Mapping[str, Any] | None:
    try:
        pointer = server.store.connection.execute(
            "SELECT mission_version_id FROM coverage_mission_pointer ORDER BY mission_ref LIMIT 1"
        ).fetchone()
    except Exception:  # noqa: BLE001 - a Core without a mission has none
        return None
    return None if pointer is None else server.coverage_mission.mission(pointer["mission_version_id"])


def dispatch(server: Any, params: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """Controller tick: refresh at most one company whose inputs moved."""

    from .return_attribution import refresh_company, refresh_industry_tags

    mission = _current_mission(server)
    if mission is None:
        return {"status": "idle", "reason": "这台 Core 上还没有 coverage mission"}
    if WRITE_SCOPE not in mission["autonomy"]["may_write"]:
        return {"status": "ungranted",
                "reason": f"任务没有授予 {WRITE_SCOPE} 写入范围，不计算利润桥与回报归因"}
    now = now or datetime.now(timezone.utc)
    connection = server.store.connection
    state = server.lane_state.setdefault(STATE_KEY, {})
    signature = input_signature(connection)
    last = state.get("at")
    due = list(state.get("due") or [])
    if not due:
        stale = last is None or now - last >= timedelta(seconds=MAX_IDLE_SECONDS)
        moved = (state.get("signature") != signature
                 and (last is None or now - last >= timedelta(seconds=MIN_SCAN_SECONDS)))
        if not (stale or moved):
            return {"status": "idle",
                    "reason": ("距上次计算不足五分钟" if state.get("signature") != signature
                               else "利润桥与回报归因的输入自上次计算以来没有变化")}
        due = sorted({member["company_ref"] for member in mission["universe"]})
        state.update({"signature": signature, "at": now, "industry_due": True})
        if not due:
            state["due"] = []
            return {"status": "idle", "reason": "任务的覆盖名单是空的"}
    company_ref = due.pop(0)
    state["due"] = due
    try:
        result = refresh_company(server.store, mission, company_ref)
        if state.pop("industry_due", False):
            result["tags_recorded"] += refresh_industry_tags(server.store, mission)
    except Exception as exc:  # noqa: BLE001 - one company's failure is not the tick's
        return {"status": "failed", "company_ref": company_ref, "remaining": len(due),
                "reason": f"计算 {company_ref} 时出错：{type(exc).__name__}: {exc}"[:MAX_DETAIL]}
    if result.get("no_inputs"):
        return {"status": "unchanged", "company_ref": company_ref, "remaining": len(due),
                "reason": f"{company_ref}：还没有申报报表行和估值快照，没有可勾稽的内容"}
    written = (result["bridges_published"] + result["series_published"]
               + result["return_published"] + result["tags_recorded"])
    reason = (f"{company_ref}：{result['bridges']} 个申报期利润桥（完整 {result['complete']}、"
              f"有差额 {result['residual']}、缺行 {result['missing_rows']}），"
              f"新版本 {result['bridges_published']}；每股序列缺 {result['per_share_missing']} 项；"
              f"回报分解可算 {result['legs_available']}/4 段；新增预期来源标签 {result['tags_recorded']}")
    return {"status": "published" if written else "unchanged", "company_ref": company_ref,
            "remaining": len(due), "reason": reason[:MAX_DETAIL],
            **{key: value for key, value in result.items() if isinstance(value, int)}}


LANE = register_lane(LaneSpec(
    operation="dispatch_return_attribution",
    # After the valuation snapshot (97) it reads; before quantitative claims (99).
    order=98,
    driver_key=DRIVER_KEY,
    handler=dispatch,
    note="ADR-0014: typed income bridges, per-share series with period semantics, "
         "holding-period return decomposition and expectation-source tags; "
         "deterministic, one company per tick, appended only on change.",
))


__all__ = ["DRIVER_KEY", "INPUT_TABLES", "LANE", "MAX_IDLE_SECONDS", "MIN_SCAN_SECONDS",
           "STATE_KEY", "WRITE_SCOPE", "dispatch", "input_signature"]
