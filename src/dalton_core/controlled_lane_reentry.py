"""Read-only eligibility for replaying a lane child after controlled redrive approval."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

from .cockpit_model import _failed_work_trace, model_failure_trace
from .controlled_failure_redrive import approved_request
from .readonly_sqlite import connect_read_only
from .scheduler import Scheduler, WorkOrder
from .store import canonical_json, content_hash

MAX_TRACES = 16
_PRODUCER_SUFFIX = re.compile(r"(:producer:[0-9a-f]{16})$")


def _validated_trace(connection: Any, value: Any) -> tuple[dict[str, Any], dict[str, Any]] | None:
    trace = model_failure_trace(SimpleNamespace(failure_trace=value))
    if trace is None:
        return None
    row = connection.execute(
        "SELECT work_order_json FROM scheduler_work_orders WHERE work_order_id=?",
        (trace["work_order_ref"],),
    ).fetchone()
    formal_row = connection.execute(
        "SELECT * FROM scheduler_formal_results WHERE work_order_id=?",
        (trace["work_order_ref"],),
    ).fetchone()
    if row is None or formal_row is None:
        return None
    try:
        work = WorkOrder.from_dict(json.loads(row["work_order_json"]))
        view = SimpleNamespace(connection=connection)
        view.work_order_authority = lambda work_id: Scheduler.work_order_authority(view, work_id)
        rebuilt = _failed_work_trace(
            view, work, purpose=trace["purpose"], request_id=trace["base_request_id"])
        formal = dict(formal_row)
        formal["result_envelope"] = json.loads(formal.pop("result_envelope_json"))
    except (KeyError, TypeError, ValueError):
        return None
    return (trace, formal) if rebuilt == trace else None


def _recovery_request_id(trace: Mapping[str, Any], suffix: str) -> str:
    request = str(trace["work_request_id"])
    producer = _PRODUCER_SUFFIX.search(request)
    if producer is None:
        return request + suffix
    return request[:producer.start()] + suffix + producer.group(1)


def _already_consumed(connection: Any, request_id: str) -> bool:
    rows = connection.execute(
        "SELECT work_order_json,work_order_hash FROM scheduler_work_orders"
    ).fetchall()
    for row in rows:
        try:
            wire = json.loads(row["work_order_json"])
        except (TypeError, ValueError):
            continue
        if (wire.get("metadata") or {}).get("request_id") != request_id:
            continue
        if (canonical_json(wire) != row["work_order_json"]
                or content_hash(wire) != row["work_order_hash"]):
            return True  # matching corrupted authority fails closed
        return True  # enqueue itself consumes the one explicit authorization
    return False


def eligible_controlled_reentries(
    summary: Mapping[str, Any], *, scheduler_db: str | Path,
    budget_db: str | Path, mission: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Return exact approved, not-yet-enqueued failed traces without writing."""

    values = summary.get("failed_model_traces")
    if not isinstance(values, list) or len(values) > MAX_TRACES:
        return []
    eligible = []
    seen = set()
    try:
        with closing(connect_read_only(scheduler_db)) as connection:
            connection.row_factory = sqlite3.Row
            for value in values:
                validated = _validated_trace(connection, value)
                if validated is None:
                    continue
                trace, formal = validated
                key = trace["work_order_ref"]
                if key in seen:
                    continue
                seen.add(key)
                suffix = approved_request(
                    scheduler_db, budget_db, old_work_order_ref=key,
                    formal=formal, mission=mission,
                )
                if suffix is None:
                    continue
                recovery_request = _recovery_request_id(trace, suffix)
                if _already_consumed(connection, recovery_request):
                    continue
                eligible.append({"failure_trace": trace,
                                 "authorization_suffix": suffix})
    except (OSError, ValueError, sqlite3.Error):
        return []
    return eligible


__all__ = ["MAX_TRACES", "eligible_controlled_reentries"]
