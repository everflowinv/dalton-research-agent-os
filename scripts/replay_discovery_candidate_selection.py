#!/usr/bin/env python3
"""Replay AlphaEngine candidate selection over one environment, read-only.

2026-09-29: every AlphaEngine acquisition on legacy and ws-7d sat behind
``selection_pending: CandidateSelectionError: candidate companies is invalid``.
This rebuilds the candidate view of every discovery that still has
``discovered`` documents, under the rules of the code it runs from, and says
how many documents can now enter selection and acquisition, and how long the
owner's AlphaEngine allowance takes to read them.

Nothing is written: ``core.sqlite`` is opened ``mode=ro``, raw pages are read
through ``RawSpoolReader``, selection tickets are read as files, no model or
connector is called.  Usage::

    PYTHONPATH=src python scripts/replay_discovery_candidate_selection.py \\
        --state-dir <state>/dalton-core [--spool-dir <state>/dalton-core/transcript-spool] \\
        [--daily-calls 30]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any

SELECTED_TYPES = {"meeting_minutes", "sell_side_report"}
OLD_LIST_BOUND = 20


def _plans(state: Path) -> dict[str, dict[str, Any]]:
    plans = {}
    for path in sorted((state / "discovery-plans").glob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(value, dict) and isinstance(value.get("id"), str) and "specs" in value:
            plans[value["id"]] = value
    return plans


def _old_rules_refuse(raw: bytes) -> str | None:
    """What b500da99 said about this page: the >20 tag-list refusal only."""

    results = json.loads(json.loads(raw.decode())["result"]["content"][0]["text"])["results"]
    for row in results:
        for field in ("companies", "industries", "markets", "sources"):
            values = row.get(field)
            if isinstance(values, list) and len(values) > OLD_LIST_BOUND:
                return f"candidate {field} is invalid"
    return None


def _selection_history(state: Path) -> dict[str, Any]:
    """Succeeded selection tickets: how many candidates a selection keeps."""

    root = state / "discovery-selections"
    by_discovery: dict[str, dict[str, Any]] = {}
    shown = kept = 0
    statuses: Counter[str] = Counter()
    for ticket_path in root.glob("*/ticket.json"):
        try:
            ticket = json.loads(ticket_path.read_text())
            source = json.loads(ticket_path.with_name("input.json").read_text())
        except (OSError, ValueError):
            continue
        statuses[str(ticket.get("status"))] += 1
        summary_path = ticket_path.with_name("summary.json")
        selected = None
        if ticket.get("status") == "succeeded" and summary_path.is_file():
            try:
                summary = json.loads(summary_path.read_text())
                selected = [row["document_ref"] for row in summary["selection"]["selected"]]
            except (OSError, ValueError, KeyError, TypeError):
                selected = None
        if selected is not None:
            shown += len(source.get("view", {}).get("candidates") or ())
            kept += len(selected)
        by_discovery[ticket.get("discovery_ref")] = {
            "status": ticket.get("status"), "selected": selected,
            "consumed": ticket_path.with_name("consumed.json").is_file()}
    return {"by_discovery": by_discovery, "statuses": dict(statuses),
            "candidates_shown": shown, "candidates_selected": kept}


def replay(state: Path, spool_dir: Path, daily_calls: int) -> dict[str, Any]:
    from dalton_core.discovery_candidate_selection import (
        SELECTION_RULES_VERSION, CandidateSelectionError, candidate_view,
    )
    from dalton_core.mission_source_discovery import MissionSourceDiscoveryCoordinator
    from dalton_core.raw_spool import RawSpoolReader

    core = sqlite3.connect(f"file:{state / 'core.sqlite'}?mode=ro", uri=True, timeout=10)
    core.row_factory = sqlite3.Row
    spool = RawSpoolReader(spool_dir)
    plans = _plans(state)
    history = _selection_history(state)
    rows = core.execute(
        "SELECT d.discovery_ref, d.mission_version_ref, s.spec_ref, s.company_ref, "
        "s.discovery_plan_ref, s.source_envelope_ref, COUNT(*) AS documents "
        "FROM coverage_mission_discovered_documents d "
        "JOIN coverage_mission_pointer p ON p.mission_version_id=d.mission_version_ref "
        "JOIN coverage_mission_source_discoveries s ON s.record_id=d.discovery_ref "
        "WHERE d.status='discovered' AND d.source_ref='source:alphaengine' "
        "GROUP BY d.discovery_ref ORDER BY MIN(d.created_at), d.discovery_ref").fetchall()
    missions: dict[str, dict[str, Any]] = {}
    report: dict[str, Any] = {
        "rules_version": SELECTION_RULES_VERSION, "discoveries": len(rows),
        "documents": sum(row["documents"] for row in rows),
        "old_rules_refused": {"discoveries": 0, "documents": 0, "reasons": Counter()},
        "new_rules": {"view_ok_discoveries": 0, "view_ok_documents": 0,
                      "would_quarantine": [], "long_lists_truncated": 0,
                      "covered_company_kept": 0, "covered_company_absent": 0},
        "not_selected_type": 0, "unreadable": [],
    }
    awaiting_selection = already_selected = consumed_documents = 0
    for row in rows:
        plan = plans.get(row["discovery_plan_ref"])
        spec = next((item for item in (plan or {}).get("specs", ())
                     if item.get("spec_ref") == row["spec_ref"]), None)
        if spec is None or spec.get("document_type") not in SELECTED_TYPES:
            report["not_selected_type"] += row["documents"]
            continue
        try:
            record = core.execute(
                "SELECT record_json FROM connector_source_envelopes WHERE source_envelope_id=?",
                (row["source_envelope_ref"],)).fetchone()
            envelope = json.loads(record["record_json"])
            raw = spool.read_object(envelope["raw_response_hash"])
        except Exception as exc:  # noqa: BLE001 - reported, never fatal
            report["unreadable"].append({"discovery_ref": row["discovery_ref"],
                                         "error": f"{type(exc).__name__}: {exc}"[:200]})
            continue
        old = _old_rules_refuse(raw)
        if old:
            report["old_rules_refused"]["discoveries"] += 1
            report["old_rules_refused"]["documents"] += row["documents"]
            report["old_rules_refused"]["reasons"][old] += 1
        version = row["mission_version_ref"]
        if version not in missions:
            wire = core.execute("SELECT record_json FROM coverage_mission_versions "
                                "WHERE mission_version_id=?", (version,)).fetchone()
            missions[version] = json.loads(wire["record_json"])
        names = MissionSourceDiscoveryCoordinator._covered_company_names(
            SimpleNamespace(plan=plan, selection_launcher=SimpleNamespace(state=state)),
            missions[version])
        try:
            view = candidate_view(raw, envelope, priority_terms=names)
        except CandidateSelectionError as exc:
            report["new_rules"]["would_quarantine"].append(
                {"discovery_ref": row["discovery_ref"], "documents": row["documents"],
                 "reason": str(exc)})
            continue
        report["new_rules"]["view_ok_discoveries"] += 1
        report["new_rules"]["view_ok_documents"] += row["documents"]
        for candidate in view["candidates"]:
            if "companies_original_count" in candidate:
                report["new_rules"]["long_lists_truncated"] += 1
                key = ("covered_company_kept" if candidate["companies_covered_kept"]
                       else "covered_company_absent")
                report["new_rules"][key] += 1
        seen = history["by_discovery"].get(row["discovery_ref"])
        if seen and seen["status"] == "succeeded" and seen["selected"] is not None:
            if seen["consumed"]:
                consumed_documents += row["documents"]
            else:
                already_selected += row["documents"]
        else:
            awaiting_selection += row["documents"]
    core.close()
    ratio = (history["candidates_selected"] / history["candidates_shown"]
             if history["candidates_shown"] else None)
    expected = None if ratio is None else round(awaiting_selection * ratio)
    report["old_rules_refused"]["reasons"] = dict(report["old_rules_refused"]["reasons"])
    report["selection"] = {
        "tickets": history["statuses"],
        "historical_keep_ratio": None if ratio is None else round(ratio, 3),
        "documents_awaiting_selection": awaiting_selection,
        "documents_in_selected_discoveries_not_consumed": already_selected,
        "documents_in_consumed_discoveries": consumed_documents,
        "expected_selected_documents": expected,
    }
    report["digest"] = {
        "alphaengine_calls_per_24h": daily_calls,
        "note": ("one get_document call per selected document; searches share the same "
                 "allowance, and acquisition goes first in every tick"),
        "days_upper_bound_all_documents": round(report["new_rules"]["view_ok_documents"]
                                                / daily_calls, 1),
        "days_expected": None if expected is None else round(expected / daily_calls, 1),
    }
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--spool-dir", type=Path,
                        help="the writer's --transcript-spool-dir (default <state>/transcript-spool)")
    parser.add_argument("--daily-calls", type=int, default=30)
    args = parser.parse_args(argv)
    state = args.state_dir.expanduser()
    spool = (args.spool_dir or state / "transcript-spool").expanduser()
    print(json.dumps(replay(state, spool, max(1, args.daily_calls)), indent=1,
                     ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
