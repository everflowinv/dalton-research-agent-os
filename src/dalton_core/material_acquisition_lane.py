"""Batch E: the material acquisition lane -- serve the MaterialRequest ledger every tick.

In process, no model, no child and no network of its own: it writes only the
request ledger and its own tables in ``material-requests.sqlite``.  Each
controller tick (:func:`material_acquisition.tick`):

1. converts what used to be dead ends into requests -- rewrite flags waiting
   for replacement material (矛盾 4) and the stable missing-material targets of
   each company's latest Dossier run; the research task lane converts the
   planner inquiries it cannot bind on its own tick;
2. reopens exhausted requests that left the 7-day negative cache (or whose
   company received new material);
3. serves at most :data:`MAX_REQUESTS_PER_TICK` open requests within
   :data:`TICK_SECONDS`, drafting products first, then P0 companies, then
   sufficiency-blocking requests, oldest first -- working each request's
   source ladder as far as it can go now.  External sources are asked through
   targeted queries their own coordinators drain on their next tick, so this
   lane never waits on a connector.

Found material is linked to its request (``material_arrived``); the redraft
thresholds and the ``requires_material`` rewrite flags read the evidence
index's ``material_arrivals`` as before and now also know which request an
arrival answered.

Registered in code (batch t's rule): every environment -- legacy, a created
workspace, a blank one -- runs it after the next deploy with no installer
step.  It runs at order 38: after the discovery coordinators (30) that drain
its targeted queries and the evidence index (37) whose new documents it
evaluates, before extraction (40) and every drafting lane.
"""

from __future__ import annotations

from typing import Any, Mapping

from .lane_registry import LaneSpec, register_lane

DRIVER_KEY = "material_acquisition"
OPERATION = "dispatch_material_acquisition"
MAX_REQUESTS_PER_TICK = 4
TICK_SECONDS = 10.0
MAX_DETAIL = 500


def dispatch(server: Any, params: Mapping[str, Any] | None = None, *,
             max_requests: int | None = None, max_seconds: float | None = None) -> dict[str, Any]:
    """Controller tick: convert, reopen, serve a bounded slice of the ledger."""

    from . import material_acquisition

    state_dir = getattr(server, "state_dir", None)
    if state_dir is None:
        return {"status": "unconfigured", "reason": "这个写入进程没有状态目录，无法获取材料"}
    try:
        summary = material_acquisition.tick(
            state_dir, max_requests=max_requests or MAX_REQUESTS_PER_TICK,
            max_seconds=max_seconds or TICK_SECONDS)
    except Exception as exc:  # noqa: BLE001 - the tick summary is where an operator reads why
        return {"status": "failed", "reason": f"材料获取出错：{type(exc).__name__}: {exc}"[:MAX_DETAIL]}
    closed = summary["found"] + summary["partial"] + summary["exhausted"] + summary["timeout"]
    if not summary["open"] and not summary["converted"] and not summary["reopened"]:
        status, reason = "idle", "没有待获取的材料请求"
    else:
        status = "served" if summary["served"] else "idle"
        reason = (f"待获取 {summary['open']} 项；本轮处理 {summary['served']} 项：找到 {summary['found']}，"
                  f"部分 {summary['partial']}，来源用尽 {summary['exhausted']}，超时 {summary['timeout']}，"
                  f"等待来源 {summary['waiting'] + summary['deferred']}，超出额度 {summary['budget_blocked']}；"
                  f"新转入 {summary['converted']} 项，重新尝试 {summary['reopened']} 项")
    out = {"status": status, "reason": reason[:MAX_DETAIL], "closed": closed}
    for key in ("open", "served", "found", "partial", "exhausted", "timeout", "waiting", "deferred",
                "budget_blocked", "converted", "reopened"):
        out[key] = int(summary.get(key) or 0)
    # Bounded refs and safe reason labels only; no question, text or error body.
    out["selection"] = summary.get("results", [])[:MAX_REQUESTS_PER_TICK]
    out["priority"] = summary.get("priority")
    for key in ("out_of_time", "scheduling_busy"):
        if summary.get(key):
            out[key] = True
    return out


LANE = register_lane(LaneSpec(
    operation=OPERATION,
    order=38,
    driver_key=DRIVER_KEY,
    handler=dispatch,
    note="Batch E: the material acquisition service -- works each MaterialRequest's source ladder "
         "(indexed corpus, sales notes, AlphaEngine, SEC incl. XBRL, Guidepoint, web) through targeted "
         "queries the source coordinators drain; negative cache, material_acquisition pool, no model.",
))


__all__ = ["DRIVER_KEY", "LANE", "MAX_REQUESTS_PER_TICK", "OPERATION", "TICK_SECONDS", "dispatch"]
