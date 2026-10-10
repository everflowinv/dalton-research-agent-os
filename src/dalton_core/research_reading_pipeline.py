"""Where each company's reading actually stands, and what is stopping it (2026-10-09).

The planner wrote "材料实际读到的深度不均 ... EPAM 有 4 份电话会逐字稿，只读了
1 份" for days.  The truth was two automatic steps, neither visible to it:

* the read count lost three proven reads to a carried review copy
  (``mission_stage._decision_signature``), and
* ACN's FY26 Q4 transcript sat open behind one orphaned model window that no
  run ever re-drove (``document_extraction_cli`` ``redriven_windows``).

This is the projection that lets it say which: per company and document kind,
how many reviews are open in the automatic queue, staged, or dismissed and
why, the open ones' blockers as the extraction lane last saw them, and how
many open rows are only superseded-version history (the "556 ACN reviews
awaiting" were 554 such rows plus 2 real ones).  ``awaiting_human_extraction``
is the review state's historical name; reading it is automatic, and nothing
here is a human gate.

Read-only and bounded.  Counts, plus at most a few named blockers.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping

MAX_BLOCKERS_PER_COMPANY = 4
MAX_KINDS_PER_COMPANY = 6


def _dismissal_kind(rationale: Any) -> str:
    text = str(rationale or "")
    if text.startswith("P13i:"):
        return "not_about_company"
    if "durably unreadable" in text:
        return "unreadable"
    if "no admissible" in text:
        return "read_nothing_admissible"
    return "other"


def _last_extraction_run(state_dir: Path | None) -> dict[str, Any] | None:
    if state_dir is None:
        return None
    root = Path(state_dir) / "extractions"
    try:
        latest = json.loads((root / "latest.json").read_text(encoding="utf-8"))
        ticket = str(latest.get("ticket") or "")
        directory = root / ticket.split(":", 1)[-1]
        summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
        if not isinstance(summary, dict):
            return None
        if latest.get("settled") is False and summary.get("status") is None:
            return None
        return summary
    except (OSError, ValueError, AttributeError):
        # A run still in flight has no summary yet; the previous one is not
        # worth guessing at.
        return None


def reading_pipeline(
    connection: sqlite3.Connection, mission_version_ref: str, *,
    state_dir: str | Path | None = None,
) -> dict[str, dict[str, Any]]:
    """company_ref -> reading position by document kind, with named blockers."""

    try:
        rows = connection.execute(
            "SELECT r.review_id,r.company_ref,r.document_ref,r.state,r.rationale,"
            "s.spec_ref FROM coverage_mission_document_reviews r "
            "LEFT JOIN coverage_mission_discovered_documents d "
            "ON d.record_id=r.discovered_document_ref "
            "LEFT JOIN coverage_mission_source_discoveries s ON s.record_id=d.discovery_ref "
            "WHERE r.mission_version_ref=?", (mission_version_ref,)).fetchall()
    except sqlite3.OperationalError:
        return {}
    out: dict[str, dict[str, Any]] = {}
    open_reviews: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        company = out.setdefault(row["company_ref"], {
            "by_kind": {}, "open_blockers": [], "superseded_open_rows": 0,
            "last_run": None})
        kind = company["by_kind"].setdefault(str(row["spec_ref"] or "unclassified"), {
            "open": 0, "staged": 0, "dismissed": {}})
        if row["state"] == "awaiting_human_extraction":
            kind["open"] += 1
            open_reviews[row["review_id"]] = row
        elif row["state"] == "extraction_staged":
            kind["staged"] += 1
        else:
            reason = _dismissal_kind(row["rationale"])
            kind["dismissed"][reason] = kind["dismissed"].get(reason, 0) + 1
    try:
        for row in connection.execute(
            "SELECT r.company_ref, COUNT(*) AS n FROM coverage_mission_document_reviews r "
            "JOIN coverage_mission_versions v ON v.mission_version_id=r.mission_version_ref "
            "JOIN coverage_mission_versions c ON c.mission_ref=v.mission_ref "
            "AND c.mission_version_id=? "
            "WHERE r.state='awaiting_human_extraction' AND r.mission_version_ref<>? "
            "GROUP BY r.company_ref", (mission_version_ref, mission_version_ref)).fetchall():
            out.setdefault(row["company_ref"], {
                "by_kind": {}, "open_blockers": [], "superseded_open_rows": 0,
                "last_run": None})["superseded_open_rows"] = int(row["n"])
    except sqlite3.OperationalError:
        pass
    excluded: dict[str, list[str]] = {}
    if open_reviews:
        try:
            marks = ",".join("?" for _ in open_reviews)
            for row in connection.execute(
                "SELECT review_id,error_code,failure_class FROM "
                f"document_extraction_window_exclusions WHERE review_id IN ({marks})",
                tuple(open_reviews)).fetchall():
                excluded.setdefault(row["review_id"], []).append(
                    f"{row['error_code']}/{row['failure_class']}")
        except sqlite3.OperationalError:
            pass
    run = _last_extraction_run(None if state_dir is None else Path(state_dir))
    skipped: dict[str, str] = {}
    redriven: dict[str, str] = {}
    run_view = None
    if run is not None:
        for item in run.get("skipped") or ():
            if isinstance(item, Mapping) and item.get("review_id"):
                skipped.setdefault(str(item["review_id"]), str(item.get("reason") or "")[:160])
        for item in run.get("redriven_windows") or ():
            if isinstance(item, Mapping) and item.get("review_id"):
                redriven[str(item["review_id"])] = str(item.get("status") or "")
        run_view = {"created_at": run.get("created_at"), "status": run.get("status"),
                    "stop_reason": run.get("stop_reason"),
                    "reviews_scanned": run.get("reviews_scanned"),
                    "reviews_complete": run.get("reviews_complete")}
    for review_id, row in open_reviews.items():
        company = out[row["company_ref"]]
        company["last_run"] = run_view
        if review_id in excluded:
            blocker = ("automatic read: windows failed permanently ("
                       + ", ".join(sorted(set(excluded[review_id]))[:3])
                       + "); the review cannot complete under this model configuration")
        elif review_id in skipped:
            blocker = "automatic read: the original could not be opened -- " + skipped[review_id]
        elif review_id in redriven:
            blocker = ("automatic read: an orphaned model window was re-driven last run "
                       f"(status {redriven[review_id] or 'unknown'})")
        elif run_view is not None and run_view.get("stop_reason") in {
                "daily_read_limit", "max_windows", "budget_rejected"}:
            blocker = f"automatic read: queued; the last run stopped on {run_view['stop_reason']}"
        else:
            blocker = "automatic read: queued for the next extraction run"
        if len(company["open_blockers"]) < MAX_BLOCKERS_PER_COMPANY:
            company["open_blockers"].append({
                "spec_ref": row["spec_ref"], "document_ref": row["document_ref"],
                "blocker": blocker})
    for company in out.values():
        kinds = sorted(company["by_kind"].items(), key=lambda item: (
            -item[1]["open"], -item[1]["staged"], item[0]))
        company["kinds_omitted"] = max(0, len(kinds) - MAX_KINDS_PER_COMPANY)
        company["by_kind"] = dict(kinds[:MAX_KINDS_PER_COMPANY])
    return out


__all__ = ["MAX_BLOCKERS_PER_COMPANY", "reading_pipeline"]
