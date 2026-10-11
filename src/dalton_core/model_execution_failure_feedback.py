"""Checked operations feedback from the existing model-lane failure ledger.

These records describe unsuccessful attempts, not financial evidence, current
model readiness, or permission to retry. No historical Work binding is inferred.
"""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
from collections.abc import Mapping
from typing import Any
from datetime import datetime, timezone
import time

from .readonly_sqlite import connect_read_only
from .store import content_hash

CONTRACT_REF = "model-execution-last-failure-feedback:0.1"
MAX_EVENTS_PER_LANE = 32
_LANES = ("mission_model_spec", "mission_model_forecast")
_FAIL_EVENTS = frozenset({"terminal", "held", "parked", "parked_again", "not_permitted"})
_CLEARED_EVENTS = frozenset({"resumed", "permission_ok", "superseded", "dependency_ok"})


def _instant(value: str) -> datetime:
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError("model execution failure timestamp lacks timezone")
    return stamp.astimezone(timezone.utc)


def _event(row: Any, *, lane: str) -> dict[str, Any]:
    body = dict(row)
    ref = body.pop("event_id")
    digest = body.pop("content_hash")
    if (body.get("lane") != lane or digest != content_hash(body)
            or ref != "lane-failure:" + digest[:32]
            or body.get("failure_class") not in {
                "dependency_unavailable", "content_refused", "not_permitted", "transient"}
            or body.get("day") != _instant(body["recorded_at"]).date().isoformat()):
        raise ValueError("model execution failure event hash or identity differs")
    return {**body, "id": ref, "content_hash": digest}


def _latest_spec_completion(connection: Any, company_ref: str) -> Mapping[str, Any] | None:
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND "
                          "name='coverage_mission_company_model_specs'").fetchone() is None:
        return None
    row = connection.execute("SELECT * FROM coverage_mission_company_model_specs "
        "WHERE company_ref=? ORDER BY created_at DESC,spec_id DESC LIMIT 1", (company_ref,)).fetchone()
    if row is None:
        return None
    from .coverage_mission import CoverageMissionAuthority
    held = CoverageMissionAuthority._model_spec_row(row)
    if held.get("schema_version") not in {"0.3", "0.4"}:
        return None  # Old shapes cannot be certified by the structured hash rule.
    body = {k: v for k, v in held.items() if k not in {
        "spec_id", "mission_version_ref", "model_profile_ref", "work_order_ref",
        "created_at", "content_hash",
    }}
    if held.get("company_ref") != company_ref or content_hash(body) != held["content_hash"]:
        raise ValueError("model execution feedback completion spec hash differs")
    return {"id": held["spec_id"], "created_at": held["created_at"],
            "content_hash": held["content_hash"]}


def last_failures(connection: Any, *, state_dir: str | Path, company_ref: str,
                  checked_forecast: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(company_ref, str) or not company_ref or "|" in company_ref:
        raise ValueError("model execution feedback needs an exact company ref")
    path = Path(state_dir) / "lane-failure-ledger.sqlite"
    if not path.is_file():
        return {"contract_ref": CONTRACT_REF, "status": "not_installed", "items": []}
    if checked_forecast is not None:
        if (checked_forecast.get("company_ref") != company_ref
                or checked_forecast.get("content_hash") != content_hash({
                    k: v for k, v in checked_forecast.items() if k != "content_hash"})):
            raise ValueError("model execution feedback forecast completion hash differs")
    accepted = {"mission_model_spec": _latest_spec_completion(connection, company_ref),
                "mission_model_forecast": checked_forecast}
    items, summaries = [], []
    with closing(connect_read_only(path)) as ledger:
        import sqlite3
        ledger.row_factory = sqlite3.Row
        ledger.execute("PRAGMA busy_timeout=250")
        deadline = time.monotonic() + 3
        ledger.set_progress_handler(lambda: time.monotonic() > deadline, 10000)
        for lane in _LANES:
            rows = ledger.execute("SELECT * FROM lane_failure_events WHERE lane=? "
                "AND item_key>=? AND item_key<? ORDER BY recorded_at DESC,event_id DESC LIMIT ?",
                (lane, company_ref + "|", company_ref + "}", MAX_EVENTS_PER_LANE + 1)).fetchall()
            events = [_event(row, lane=lane) for row in rows]
            seen, active = set(), []
            for event in events[:MAX_EVENTS_PER_LANE]:
                key = event["item_key"]
                if not key.startswith(company_ref + "|"):
                    raise ValueError("model execution failure company prefix differs")
                if key in seen:
                    continue
                seen.add(key)
                if event["event"] in _CLEARED_EVENTS:
                    continue
                if event["event"] not in _FAIL_EVENTS:
                    raise ValueError("model execution failure event kind is unknown")
                completion = accepted[lane]
                if completion and _instant(completion["created_at"]) >= _instant(event["recorded_at"]):
                    continue
                if event.get("dependency"):
                    cleared = ledger.execute("SELECT * FROM lane_failure_events WHERE dependency=? "
                        "AND event='dependency_ok' AND recorded_at>=? "
                        "ORDER BY recorded_at DESC,event_id DESC LIMIT 1",
                        (event["dependency"], event["recorded_at"])).fetchone()
                    if cleared is not None:
                        _event(cleared, lane=cleared["lane"])
                        continue
                active.append(event)
            if active:
                chosen = active[0]
                items.append({"id": chosen["id"], "content_hash": chosen["content_hash"],
                    "lane": lane, "company_ref": company_ref, "item_key": chosen["item_key"],
                    "recorded_at": chosen["recorded_at"], "reason": chosen["reason"],
                    "failure_class": chosen["failure_class"], "status": chosen["status"],
                    "rule": chosen["rule"], "scope": "operations_last_failure_only",
                    "financial_evidence": False, "retry_authority": False,
                    "work_formal_binding": "not_linked_by_this_ledger"})
            omitted = [event["content_hash"] for event in active[1:]]
            summaries.append({"lane": lane, "events_read": len(events),
                "event_window_truncated": len(events) > MAX_EVENTS_PER_LANE,
                "additional_active_failures_in_window": len(omitted),
                "additional_active_failures_hash": content_hash(omitted),
                "unprocessed_tail_hash": content_hash([e["content_hash"] for e in events[MAX_EVENTS_PER_LANE:]])})
    body = {"contract_ref": CONTRACT_REF, "status": "checked", "items": items,
            "window_summary": summaries}
    return {**body, "content_hash": content_hash(body)}
