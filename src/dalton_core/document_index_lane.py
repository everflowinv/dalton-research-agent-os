"""Batch A (证据底座): keep the full-text evidence index current, every tick.

In process, no model, no network, no child, no grant -- it writes nothing but
``document-index.sqlite``, a projection beside the Core.  Each controller tick
calls :func:`evidence_index.index_tick`, which picks up every acquisition
manifest written since the last tick (that is "index on acquisition
completion") and, on a Core that never had an index, works through the
backfill newest-first within a bounded slice of documents and seconds,
resumable from its ledger.

Registered in code (batch t's rule): the writer derives its lane set from
:mod:`lane_registry` at start-up, so every environment -- legacy, a created
workspace, a blank one -- runs this lane after the next deploy with no
installer step and no plist edit.  A Core with no acquisitions yet answers
``idle`` with a Chinese reason.

It runs at order 37: after the acquisition lanes (discovery 30, prior research
32, Guidepoint 35) whose manifests it reads, before document extraction (40)
and every drafting lane that will search it.
"""

from __future__ import annotations

from typing import Any, Mapping

from .lane_registry import LaneSpec, register_lane

DRIVER_KEY = "document_index"
OPERATION = "dispatch_document_index"
MAX_DETAIL = 500


def dispatch(server: Any, params: Mapping[str, Any] | None = None, *,
             max_documents: int | None = None, max_seconds: float | None = None) -> dict[str, Any]:
    """Controller tick: index what arrived, then a bounded slice of the backfill."""

    from . import evidence_index

    state_dir = getattr(server, "state_dir", None)
    if state_dir is None:
        return {"status": "unconfigured", "reason": "这个写入进程没有状态目录，无法建立全文证据索引"}
    try:
        summary = evidence_index.index_tick(
            state_dir,
            max_documents=max_documents or evidence_index.TICK_MAX_DOCUMENTS,
            max_seconds=max_seconds or evidence_index.TICK_MAX_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 - the tick summary is where an operator reads why
        return {"status": "failed",
                "reason": f"全文证据索引出错：{type(exc).__name__}: {exc}"[:MAX_DETAIL]}
    touched = summary["indexed"] + summary["skipped"] + summary["failed"] + summary["already_indexed"]
    if not touched and not summary["companies_synced"]:
        status = "idle"
        reason = "没有新获取的资料需要建立全文索引"
    else:
        status = "failed" if summary["failed"] and not summary["indexed"] else "indexed"
        reason = (f"新索引 {summary['indexed']} 份资料（{summary['passages']} 段），"
                  f"跳过 {summary['skipped']}，失败 {summary['failed']}，"
                  f"新增到达记录 {summary['arrivals']}；待索引 {summary['remaining']}")
    return {"status": status, "reason": reason[:MAX_DETAIL],
            **{key: value for key, value in summary.items() if isinstance(value, (int, float))}}


LANE = register_lane(LaneSpec(
    operation=OPERATION,
    order=37,
    driver_key=DRIVER_KEY,
    handler=dispatch,
    note="Batch A: passage-level FTS5 evidence index over every acquired document body "
         "(SEC filings and 8-K exhibits, transcripts, broker reports, sales notes, Guidepoint, "
         "web pages) plus the append-only material-arrivals ledger; bounded, resumable, no model.",
))


__all__ = ["DRIVER_KEY", "LANE", "OPERATION", "dispatch"]
