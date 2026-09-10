"""P10a (vision v1.1): drive the mission by the Playbook's stages.

Phase 9 wrote the team's methodology into a contract: every company walks
Initial Screen → Deep Insight Gate → industry model → company model →
Investment Memo → active coverage, and each stage carries required readings,
required outputs and an exit gate.  P9d then made "search → acquire → read →
admit a Claim" fully autonomous.  Nothing joined the two: the live stage
ledger was empty, and the lanes picked their next document by age alone, so
the AlphaEngine day budget went to whichever company was discovered first
(live: every transcript belonged to EPAM, every broker report to Cognizant,
while the P0 company had neither).

This module is the join.  It does three things, all deterministic:

- **Enters the first stage.**  A company with no stage record gets
  ``initial_screen entered`` under the mission's own automation principal.
  Nothing here passes a gate; passing needs the deliverable (P10c).
- **Scores the source base.**  The Playbook's Initial Screen required
  readings ("过去 4 个季度财报与电话会、最新年报或招股书、近 6 个月多空券商观点")
  become four counted items.  A document counts for an item when the
  discovery spec that found it declares that document type, which the
  discovery record already stores; nothing is inferred from the text.
- **Orders the lanes by gap.**  ``acquisition_needs`` ranks (company, spec)
  pairs by the mission's own bootstrap priority and by what each company is
  still missing, so the governed daily calls buy the missing readings of the
  most important company first.

No authority is invented: the counts are a projection over the mission's own
tables, and the only write is the stage record the mission already grants.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .coverage_mission import CoverageMissionError, STAGE_ORDER, fold_stage_status

SCHEMA_VERSION = "0.1"
FIRST_STAGE = "initial_screen"
ACQUIRED_STATUSES = ("acquired", "already_in_authority")
READ_REVIEW_STATE = "extraction_staged"

STAGE_LABELS: dict[str, str] = {
    "initial_screen": "初步筛选",
    "deep_insight_gate": "深度认知门",
    "industry_model": "行业模型",
    "company_model": "公司模型",
    "investment_memo": "投资备忘录",
    "active_coverage": "持续覆盖",
}
STAGE_STATUS_LABELS: dict[str, str] = {
    "entered": "进行中",
    "gate_passed": "已通过",
    "gate_failed": "未通过，正在补",
    # P14d sequel: passed once, and a person has approved re-opening it. The
    # label says "again" rather than "in progress" because the difference
    # between a first pass and a second one is the whole point of the version
    # chain, and a page that hid it would hide that.
    "reopened": "已通过，但要重出一版",
}

# The Playbook's Initial Screen required_readings, translated into items a
# machine can count.  ``spec_refs`` names the discovery specs whose declared
# document type satisfies the item; an item with no planned spec is reported
# as ``not_planned`` rather than silently missing.
SOURCE_BASE_ITEMS: tuple[dict[str, Any], ...] = (
    {
        "item_ref": "quarterly_financials",
        "label": "过去 4 个季度的财报数字",
        "reading": "读过去 4 个季度财报",
        "required": 4,
        "counted_by": "quantitative_claim_periods",
        "source_ref": "source:sec-edgar",
        "spec_refs": (),
    },
    {
        "item_ref": "earnings_calls",
        "label": "过去 4 个季度的电话会纪要",
        "reading": "读过去 4 个季度电话会",
        "required": 4,
        "counted_by": "acquired_documents",
        "source_ref": "source:alphaengine",
        "spec_refs": ("earnings-call-transcripts",),
    },
    {
        # AlphaEngine's own document-type catalogue carries calls and research
        # only; a 10-K has to come from SEC EDGAR, where the connected lane
        # reads XBRL facts and not filing text.  The item says exactly that
        # instead of looking like a search that was never run.
        "item_ref": "annual_report",
        "label": "最新年报（10-K）正文",
        "reading": "读最新年报或招股书",
        "required": 1,
        "counted_by": "acquired_documents",
        "source_ref": "source:sec-edgar",
        # P10u: the channel exists now -- the filings index names the 10-K and
        # the public-web fetch lane retrieves it. The note below is still the
        # honest answer on an install whose discovery plans do not carry this
        # spec, because there the item genuinely has no route.
        "spec_refs": ("annual-report-10k",),
        "gap_note": "还没有获取 10-K 正文的通道；现在只从 SEC 取了财报数字",
    },
    {
        "item_ref": "broker_research",
        "label": "近 6 个月的多空券商观点",
        "reading": "读近 6 个月多空券商观点",
        "required": 3,
        "counted_by": "acquired_documents",
        "source_ref": "source:alphaengine",
        "spec_refs": ("sell-side-reports",),
    },
)
# P13f: what the screen needs that belongs to no company.
#
# An industry Initial Screen rests on facts about the market -- how demand is
# moving, how the field is arranged -- and those are not any one company's.
# The discovery plan already asks both questions, but it asks them *per
# company*: "{terms} IT services demand bookings outlook" runs five times with
# five different companies' names, and every market-sizing document it finds is
# filed under whichever company's search happened to return it.
#
# So the same documents are counted here against the industry, where they
# actually belong. Nothing new is fetched; what changes is that the industry
# has a checklist of its own, its gaps are visible, and a fact about the market
# has somewhere to live that is not a company's file.
INDUSTRY_BASE_ITEMS: tuple[dict[str, Any], ...] = (
    {
        "item_ref": "industry_demand",
        "label": "行业需求与支出趋势",
        "reading": "读行业需求与支出趋势",
        "required": 3,
        "counted_by": "acquired_documents",
        "source_ref": "source:web-search",
        "spec_refs": ("industry-demand",),
    },
    {
        "item_ref": "competitive_landscape",
        "label": "行业竞争格局",
        "reading": "读行业竞争格局",
        "required": 3,
        "counted_by": "acquired_documents",
        "source_ref": "source:web-search",
        "spec_refs": ("competitive-landscape",),
    },
)

_ITEM_ORDER = {item["item_ref"]: index for index, item in enumerate(SOURCE_BASE_ITEMS)}
# Reading order inside one company, used by the extraction lane: an original
# that carries management's own words before someone else's summary of them.
SPEC_READING_RANK: dict[str, int] = {
    "earnings-call-transcripts": 0,
    "annual-reports": 1,
    "sell-side-reports": 2,
}
DEFAULT_SPEC_RANK = 3
PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


class MissionStageError(RuntimeError):
    """A stage driver refusal; the message is safe to show."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def company_priority_order(mission: Mapping[str, Any]) -> list[str]:
    """Company refs ordered by the mission's own bootstrap priority, then universe order."""

    indexed = list(enumerate(mission["universe"]))
    indexed.sort(key=lambda pair: (PRIORITY_ORDER.get(pair[1].get("bootstrap_priority"), 9), pair[0]))
    return [member["company_ref"] for _, member in indexed]


def planned_spec_refs(plans: Sequence[Mapping[str, Any]]) -> set[str]:
    """Every spec ref the given discovery plans declare."""

    result: set[str] = set()
    for plan in plans:
        for spec in (plan or {}).get("specs") or ():
            ref = spec.get("spec_ref") if isinstance(spec, Mapping) else None
            if isinstance(ref, str) and ref:
                result.add(ref)
    return result


def planned_spec_refs_from_directory(directory: Path) -> set[str]:
    """Spec refs from every readable discovery plan in a directory (best effort)."""

    plans: list[Mapping[str, Any]] = []
    try:
        entries = sorted(directory.glob("*.json"))
    except OSError:
        return set()
    for path in entries:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(value, Mapping):
            plans.append(value)
    return planned_spec_refs(plans)


# P10f: how far a document got, worst to best.  A document can appear under
# several mission versions with different statuses, because carry-forward
# re-registers unfinished rows under the new version; the mission holds
# whatever the *best* of those rows reached.
_STATUS_RANK = {
    "acquisition_failed": 1,
    "discovered": 2,
    "acquisition_launched": 2,
    "acquired": 3,
    "already_in_authority": 3,
}


def _document_counts(
    connection: sqlite3.Connection, mission_version_ref: str,
    *, company_name_keys: Mapping[str, str] | None = None,
) -> dict[tuple[str, str], dict[str, int]]:
    """(company_ref, spec_ref) → acquired / pending / failed / read counts.

    The checklist asks an *inventory* question — does the mission hold four
    transcripts for ACN — but ``coverage_mission_discovered_documents`` is
    maintained as an outstanding-work queue: ``carry_forward_superseded_documents``
    deliberately does not copy a document whose review was already resolved,
    because nothing is owed on it.  Counting only the current version therefore
    made the checklist *fall* when work finished: live, five ACN transcripts
    read under v8 left the count at v9 and 纪要 went 20 → 15.

    So count every version of the same mission and deduplicate by
    ``document_ref``, keeping the furthest each document ever got.  A document
    the mission acquired under v8 is still a document the mission holds under
    v9, whether or not v9 has any work left to do on it.
    """

    counts: dict[tuple[str, str], dict[str, Any]] = {}

    def bucket(company: str, spec: str) -> dict[str, int]:
        return counts.setdefault(
            (company, spec), {"acquired": 0, "pending": 0, "failed": 0,
                              "read": 0, "periods": [], "required_periods": [],
                              "missing_periods": [], "unclassified": 0,
                              "not_attributed": 0}
        )

    row = connection.execute(
        "SELECT mission_ref FROM coverage_mission_versions WHERE mission_version_id=?",
        (mission_version_ref,),
    ).fetchone()
    if row is None:
        # No version row to resolve a mission from; count the one version we
        # were handed rather than silently counting nothing.
        scope, params = "d.mission_version_ref=?", (mission_version_ref,)
        review_scope, review_params = "r.mission_version_ref=?", (mission_version_ref,)
    else:
        scope = (
            "d.mission_version_ref IN (SELECT mission_version_id "
            "FROM coverage_mission_versions WHERE mission_ref=?)"
        )
        params = (row["mission_ref"],)
        review_scope = (
            "r.mission_version_ref IN (SELECT mission_version_id "
            "FROM coverage_mission_versions WHERE mission_ref=?)"
        )
        review_params = (row["mission_ref"],)

    latest_reviews: dict[tuple[str, str], tuple[str, str]] = {}
    for review in connection.execute(
        "SELECT company_ref, document_ref, state, updated_at, review_id "
        "FROM coverage_mission_document_reviews "
        f"WHERE {review_scope.replace('r.', '')} ORDER BY updated_at, review_id",
        review_params,
    ).fetchall():
        latest_reviews[(review["company_ref"], review["document_ref"])] = (
            review["state"], review["review_id"]
        )

    # A source can return the same external document for several companies or
    # specs.  Those are separate attribution claims.  Deduplicating on the
    # external id alone made an equal-rank row belong to whichever company
    # SQLite happened to return first.
    best: dict[tuple[str, str, str], str] = {}
    for entry in connection.execute(
        "SELECT d.document_ref AS document_ref, d.company_ref AS company_ref, "
        "s.spec_ref AS spec_ref, d.status AS status "
        "FROM coverage_mission_discovered_documents d "
        "JOIN coverage_mission_source_discoveries s ON s.record_id=d.discovery_ref "
        f"WHERE {scope}",
        params,
    ).fetchall():
        document_ref, status = entry["document_ref"], entry["status"]
        if latest_reviews.get((entry["company_ref"], document_ref), (None, None))[0] == "dismissed":
            continue
        key = (entry["company_ref"], entry["spec_ref"], document_ref)
        rank = _STATUS_RANK.get(status, 0)
        current = best.get(key)
        if current is None or rank > _STATUS_RANK.get(current, 0):
            best[key] = status
    try:
        provenance_rows = connection.execute(
            "SELECT document_ref,title,named_companies_json,metadata_seen "
            "FROM document_provenance_records"
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        provenance_rows = []
    provenance = {row["document_ref"]: row for row in provenance_rows}
    earnings_periods: dict[tuple[str, str], set[str]] = {}
    document_period: dict[tuple[str, str], str] = {}
    for (company_ref, spec_ref, document_ref), status in best.items():
        entry_counts = bucket(company_ref, spec_ref)
        if status in ACQUIRED_STATUSES:
            if spec_ref == "earnings-call-transcripts":
                source_metadata = provenance.get(document_ref)
                if source_metadata is None or not source_metadata["metadata_seen"]:
                    entry_counts["not_attributed"] += 1
                    continue
                title = None if source_metadata is None else source_metadata["title"]
                from .document_subject import document_names_subject, earnings_call_names_issuer
                name_key = (company_name_keys or {}).get(company_ref)
                attribution = earnings_call_names_issuer(title, name_key)
                try:
                    named_companies = json.loads(source_metadata["named_companies_json"])
                except (TypeError, ValueError, json.JSONDecodeError):
                    named_companies = []
                named_attribution = document_names_subject(
                    " | ".join(str(value) for value in named_companies), name_key)
                if (not attribution.get("checked")
                        or not attribution.get("names_issuer")
                        or not named_attribution.get("names_subject")):
                    entry_counts["not_attributed"] += 1
                    continue
                period = _earnings_call_period(title)
                if period is None:
                    entry_counts["unclassified"] += 1
                    continue
                document_period[(company_ref, document_ref)] = period
                periods = earnings_periods.setdefault((company_ref, spec_ref), set())
                if period not in periods:
                    periods.add(period)
                    entry_counts["periods"] = sorted(periods)
            else:
                entry_counts["acquired"] += 1
        elif status in ("discovered", "acquisition_launched"):
            entry_counts["pending"] += 1
        elif status == "acquisition_failed":
            entry_counts["failed"] += 1

    read_docs: set[tuple[str, str, str]] = set()
    for entry in connection.execute(
        "SELECT d.document_ref AS document_ref, d.company_ref AS company_ref, "
        "s.spec_ref AS spec_ref FROM coverage_mission_document_reviews r "
        "JOIN coverage_mission_discovered_documents d ON d.record_id=r.discovered_document_ref "
        "JOIN coverage_mission_source_discoveries s ON s.record_id=d.discovery_ref "
        f"WHERE {review_scope} AND r.state=?",
        (*review_params, READ_REVIEW_STATE),
    ).fetchall():
        key = (entry["company_ref"], entry["document_ref"])
        if latest_reviews.get(key, (None, None))[0] == READ_REVIEW_STATE:
            read_docs.add((entry["company_ref"], entry["spec_ref"], entry["document_ref"]))
    read_periods: dict[tuple[str, str], set[str]] = {}
    for company_ref, spec_ref, _document_ref in read_docs:
        if spec_ref == "earnings-call-transcripts":
            period = document_period.get((company_ref, _document_ref))
            if period is None:
                continue
            periods = read_periods.setdefault((company_ref, spec_ref), set())
            if period in periods:
                continue
            periods.add(period)
        else:
            bucket(company_ref, spec_ref)["read"] += 1
    for key, periods in earnings_periods.items():
        entry_counts = bucket(*key)
        required = _required_quarters(max(periods, key=_quarter_ordinal))
        entry_counts["required_periods"] = required
        entry_counts["missing_periods"] = [period for period in required
                                            if period not in periods]
        entry_counts["acquired"] = sum(period in periods for period in required)
        entry_counts["read"] = sum(period in read_periods.get(key, set())
                                   for period in required)
    return counts


_QUARTER_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9])Q([1-4])\s*['’/-]?\s*(20\d{2})(?!\d)", re.I),
    re.compile(r"(?<!\d)(20\d{2})\s*['’/-]?\s*Q([1-4])(?![A-Za-z0-9])", re.I),
    re.compile(r"(?<![A-Za-z0-9])([1-4])Q\s*['’/-]?\s*(20\d{2})(?!\d)", re.I),
)


def _earnings_call_period(title: Any) -> str | None:
    """Fiscal quarter explicitly asserted by trusted source metadata."""
    if not isinstance(title, str) or not title.strip():
        return None
    lowered = title.casefold()
    if "fireside" in lowered:
        return None
    if "conference" in lowered and not (
            "earnings conference call" in lowered or "earnings call" in lowered):
        return None
    for index, pattern in enumerate(_QUARTER_PATTERNS):
        match = pattern.search(title)
        if match:
            if index == 1:
                year, quarter = match.group(1), match.group(2)
            else:
                quarter, year = match.group(1), match.group(2)
            return f"FY{year}-Q{quarter}"
    return None


def _quarter_ordinal(period: str) -> int:
    match = re.fullmatch(r"FY(20\d{2})-Q([1-4])", period)
    if match is None:
        raise MissionStageError("invalid classified fiscal quarter")
    return int(match.group(1)) * 4 + int(match.group(2)) - 1


def _required_quarters(latest: str) -> list[str]:
    end = _quarter_ordinal(latest)
    return [f"FY{ordinal // 4}-Q{ordinal % 4 + 1}"
            for ordinal in range(end - 3, end + 1)]


def retired_claim_refs(connection: sqlite3.Connection) -> set[str]:
    """P10b: claim versions a challenge decision retired, or none on an older Core."""

    try:
        rows = connection.execute(
            "SELECT claim_version_ref FROM claim_retirement_decisions WHERE decision='retired'"
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return set()
        raise
    return {row["claim_version_ref"] for row in rows}


def _claim_periods(connection: sqlite3.Connection) -> dict[str, set[str]]:
    """company_ref → distinct periods asserted by a live quantitative Claim."""

    periods: dict[str, set[str]] = {}
    retired = retired_claim_refs(connection)
    rows = connection.execute(
        "SELECT claim_version_id AS id, json_extract(claim_json,'$.subject_ref') AS subject_ref, "
        "json_extract(claim_json,'$.period') AS period FROM claim_versions "
        "WHERE json_extract(claim_json,'$.value') IS NOT NULL"
    ).fetchall()
    for row in rows:
        if row["id"] in retired:
            continue
        subject, period = row["subject_ref"], row["period"]
        if isinstance(subject, str) and isinstance(period, str) and period:
            periods.setdefault(subject, set()).add(period)
    return periods


def _item_status(
    item: Mapping[str, Any], have: int, *, connected: bool, planned: bool
) -> tuple[str, str]:
    required = int(item["required"])
    if have >= required:
        return "complete", f"已有 {have} 份，够了"
    if not connected:
        return "source_unavailable", f"{item['source_ref']} 还没有接入，这项拿不到"
    if item["counted_by"] == "acquired_documents" and (not item["spec_refs"] or not planned):
        return "not_planned", item.get("gap_note") or "还没有对应的搜索规格，需要先加一条"
    if have == 0:
        return "missing", f"一份都没有，还差 {required} 份"
    return "partial", f"已有 {have} 份，还差 {required - have} 份"


def evaluate_mission(
    connection: sqlite3.Connection,
    mission: Mapping[str, Any],
    *,
    planned_specs: set[str] | None = None,
    stage_state: Mapping[str, Mapping[str, Sequence[str]]] | None = None,
) -> list[dict[str, Any]]:
    """The Initial Screen source base, per company, as plain counted items."""

    planned = set(planned_specs or set())
    counts = _document_counts(
        connection, mission["id"],
        company_name_keys={member["company_ref"]: member.get("ticker", "")
                           for member in mission["universe"]},
    )
    periods = _claim_periods(connection)
    connected = {
        entry["source_ref"]
        for entry in mission["source_plan"]
        if entry.get("status") == "connected"
    }
    order = company_priority_order(mission)
    members = {member["company_ref"]: member for member in mission["universe"]}
    result: list[dict[str, Any]] = []
    for company_ref in order:
        member = members[company_ref]
        history = (stage_state or {}).get(company_ref) or {}
        entered = [stage for stage in STAGE_ORDER if history.get(stage)]
        stage = entered[-1] if entered else None
        # P14-S: the last *decision* is the status, not the last record and
        # not "gate_passed appears somewhere". A reopened gate's gate_failed
        # comes after its gate_passed and must win.
        status = None if stage is None else fold_stage_status(list(history.get(stage) or ()))
        items = []
        for item in SOURCE_BASE_ITEMS:
            if item["counted_by"] == "quantitative_claim_periods":
                have = len(periods.get(company_ref, set()))
                read = have
                pending = failed = 0
            else:
                have = read = pending = failed = unclassified = not_attributed = 0
                classified_periods: set[str] = set()
                required_periods: set[str] = set()
                missing_periods: set[str] = set()
                for spec in item["spec_refs"]:
                    entry = counts.get((company_ref, spec))
                    if entry is None:
                        continue
                    have += entry["acquired"]
                    read += entry["read"]
                    pending += entry["pending"]
                    failed += entry["failed"]
                    unclassified += int(entry.get("unclassified") or 0)
                    not_attributed += int(entry.get("not_attributed") or 0)
                    classified_periods.update(entry.get("periods") or ())
                    required_periods.update(entry.get("required_periods") or ())
                    missing_periods.update(entry.get("missing_periods") or ())
            item_status, note = _item_status(
                item,
                have,
                connected=item["source_ref"] in connected,
                planned=all(spec in planned for spec in item["spec_refs"]) if item["spec_refs"] else True,
            )
            if item_status in {"partial", "missing"} and pending:
                note += f"；还有 {pending} 份已找到、排队等取"
            items.append({
                "item_ref": item["item_ref"], "label": item["label"], "reading": item["reading"],
                "required": int(item["required"]), "have": have, "read": read, "pending": pending,
                "failed": failed, "status": item_status, "note": note,
                "source_ref": item["source_ref"], "spec_refs": list(item["spec_refs"]),
                **({"classified_periods": sorted(classified_periods),
                    "required_periods": sorted(required_periods),
                    "missing_periods": sorted(missing_periods),
                    "period_coverage": "known" if required_periods else "unknown",
                    "unclassified": unclassified,
                    "not_attributed": not_attributed}
                   if item["item_ref"] == "earnings_calls" else {}),
            })
        blocking = [i for i in items if i["status"] in {"partial", "missing"}]
        result.append({
            "company_ref": company_ref, "ticker": member.get("ticker"),
            "priority": member.get("bootstrap_priority"), "tier": member.get("coverage_tier"),
            "stage": stage, "stage_label": STAGE_LABELS.get(stage or "", "还没开始"),
            "stage_status": status, "stage_status_label": STAGE_STATUS_LABELS.get(status or "", "还没开始"),
            "items": items,
            "source_base_ready": all(item["status"] == "complete" for item in items),
            "gaps": [i["item_ref"] for i in blocking],
            "blocked_on": [i["item_ref"] for i in items if i["status"] in {"not_planned", "source_unavailable"}],
        })
    return result


def evaluate_industry(
    connection: sqlite3.Connection,
    mission: Mapping[str, Any],
    *,
    planned_specs: set[str] | None = None,
) -> dict[str, Any]:
    """The industry's own source base, counted across the whole mission.

    The per-company checklist cannot hold these: a market-demand report is not
    Accenture's, and filing it under Accenture is how five copies of the same
    industry research end up looking like five companies' progress.

    Counted across every company because that is how the documents were found
    -- the plan runs the industry queries once per company -- while the subject
    of what they say is the industry.
    """

    planned = set(planned_specs or set())
    counts = _document_counts(connection, mission["id"])
    connected = {
        entry["source_ref"] for entry in mission.get("source_plan", ())
        if entry.get("status") == "connected"
    }
    items = []
    for item in INDUSTRY_BASE_ITEMS:
        have = read = pending = failed = 0
        wanted = set(item["spec_refs"])
        for (_company_ref, spec_ref), entry in counts.items():
            if spec_ref not in wanted:
                continue
            have += entry["acquired"]
            read += entry["read"]
            pending += entry["pending"]
            failed += entry["failed"]
        status, note = _item_status(
            item, have,
            connected=item["source_ref"] in connected,
            planned=all(spec in planned for spec in item["spec_refs"]),
        )
        items.append({
            "item_ref": item["item_ref"], "label": item["label"],
            "reading": item["reading"], "required": int(item["required"]),
            "have": have, "read": read, "pending": pending, "failed": failed,
            "status": status, "note": note, "source_ref": item["source_ref"],
            "spec_refs": list(item["spec_refs"]),
        })
    blocking = [i for i in items if i["status"] in {"partial", "missing"}]
    return {
        "industry_ref": mission.get("industry_ref"),
        "items": items,
        "gaps": [i["item_ref"] for i in blocking],
        "blocked_on": [i["item_ref"] for i in items
                       if i["status"] in {"not_planned", "source_unavailable"}],
        "source_base_ready": all(item["status"] == "complete" for item in items),
    }


def acquisition_needs(
    companies: Sequence[Mapping[str, Any]], *, source_ref: str | None = None
) -> list[dict[str, Any]]:
    """(company, spec) pairs still missing readings, most important first.

    One pass per company in mission priority order, then by the Playbook's own
    reading order: the P0 company's missing transcripts outrank the P2
    company's fourth broker report.
    """

    needs: list[dict[str, Any]] = []
    for rank, company in enumerate(companies):
        for item in company["items"]:
            if item["status"] not in {"partial", "missing"} or not item["spec_refs"]:
                continue
            if source_ref is not None and item["source_ref"] != source_ref:
                continue
            if not item["pending"]:
                continue  # nothing discovered to acquire; discovery must find it first
            for spec in item["spec_refs"]:
                needs.append({
                    "company_ref": company["company_ref"], "spec_ref": spec,
                    "item_ref": item["item_ref"], "deficit": item["required"] - item["have"],
                    "company_rank": rank, "item_rank": _ITEM_ORDER[item["item_ref"]],
                })
    needs.sort(key=lambda need: (need["company_rank"], need["item_rank"], need["spec_ref"]))
    return needs


def discovery_needs(
    companies: Sequence[Mapping[str, Any]], *, source_ref: str | None = None
) -> list[dict[str, Any]]:
    """(company, spec) pairs whose gap cannot be closed by what is already found."""

    needs: list[dict[str, Any]] = []
    for rank, company in enumerate(companies):
        for item in company["items"]:
            if item["status"] not in {"partial", "missing"} or not item["spec_refs"]:
                continue
            if source_ref is not None and item["source_ref"] != source_ref:
                continue
            if item["pending"] >= item["required"] - item["have"]:
                continue  # enough already discovered; the acquisition lane will close it
            for spec in item["spec_refs"]:
                needs.append({
                    "company_ref": company["company_ref"], "spec_ref": spec,
                    "item_ref": item["item_ref"],
                    "deficit": item["required"] - item["have"] - item["pending"],
                    "company_rank": rank, "item_rank": _ITEM_ORDER[item["item_ref"]],
                })
    needs.sort(key=lambda need: (need["company_rank"], need["item_rank"], need["spec_ref"]))
    return needs


def review_sort_key(
    review: Mapping[str, Any],
    *,
    company_rank: Mapping[str, int],
    spec_by_document: Mapping[str, str],
) -> tuple[int, int, str, str]:
    """Reading order for the extraction lane: priority company, then original kind."""

    spec = spec_by_document.get(review.get("document_ref") or "", "")
    return (
        company_rank.get(review.get("company_ref") or "", len(company_rank)),
        SPEC_READING_RANK.get(spec, DEFAULT_SPEC_RANK),
        str(review.get("created_at") or ""),
        str(review.get("review_id") or ""),
    )


class MissionStageDriver:
    """Enter the first stage and report the source base for every active mission."""

    def __init__(
        self,
        missions: Any,
        *,
        planned_specs: set[str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.missions = missions
        self.planned_specs = set(planned_specs or set())
        self.clock = clock or _now

    def _active_missions(self) -> list[dict[str, Any]]:
        rows = self.missions.connection.execute(
            "SELECT mission_version_id FROM coverage_mission_pointer ORDER BY mission_ref"
        ).fetchall()
        return [self.missions.mission(row["mission_version_id"]) for row in rows]

    def _stage_state(self, mission: Mapping[str, Any]) -> dict[str, dict[str, list[str]]]:
        """The ladder folded across every version of this mission (P14-S).

        Read by ``mission_ref`` rather than by the active version id.  Reading
        the active version made the checklist forget the ladder on every
        publish: live, v7 through v13 each carry their own five ``entered``
        rows for the same five companies, and the four Initial Screens that
        passed under v13 would read as never-screened under v14.
        """

        return self.missions.stage_state_by_company(mission["mission_ref"])

    def evaluate(self) -> dict[str, Any]:
        missions = []
        for mission in self._active_missions():
            companies = evaluate_mission(
                self.missions.connection, mission,
                planned_specs=self.planned_specs, stage_state=self._stage_state(mission),
            )
            missions.append({
                "mission_ref": mission["mission_ref"], "mission_version_ref": mission["id"],
                "title": mission["title"], "companies": companies,
                "stage_order": [
                    {"stage_ref": stage, "label": STAGE_LABELS.get(stage, stage)}
                    for stage in STAGE_ORDER
                ],
            })
        return {
            "projection_kind": "mission_stage_checklist", "schema_version": SCHEMA_VERSION,
            "as_of": self.clock().isoformat(timespec="seconds"), "missions": missions,
        }

    def needs(self, *, source_ref: str | None = None) -> list[dict[str, Any]]:
        """Acquisition needs across active missions, most important first."""

        result: list[dict[str, Any]] = []
        for mission in self._active_missions():
            companies = evaluate_mission(
                self.missions.connection, mission,
                planned_specs=self.planned_specs, stage_state=self._stage_state(mission),
            )
            result.extend(acquisition_needs(companies, source_ref=source_ref))
        return result

    def run_once(self) -> dict[str, Any]:
        """Enter ``initial_screen`` for every company that has no stage record yet.

        P14-S: "yet" means under *any* version of the mission, not under the
        active one.  The decision is to stop re-seeding rather than to seed a
        provenance-only duplicate: a second ``entered`` under v14 says nothing
        the v7 one did not, it cannot move the folded state (``entered`` never
        supersedes a decision), and it made an append-only ledger grow five
        rows per publish -- live, 30 of the 41 stage records are re-seeds of
        the same five ``initial_screen entered`` facts.  A company that has
        entered has entered.  ``record_stage`` refuses the duplicate anyway
        now that it folds; skipping it keeps the tick silent instead of
        reporting five refusals it caused itself.
        """

        entered: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        missions = []
        for mission in self._active_missions():
            state = self._stage_state(mission)
            automation = mission["autonomy"]["automation_principal"]
            for company_ref in company_priority_order(mission):
                if state.get(company_ref):
                    continue
                try:
                    record = self.missions.record_stage(
                        mission_version_ref=mission["id"],
                        mission_version_hash=mission["content_hash"],
                        company_ref=company_ref, stage_ref=FIRST_STAGE, status="entered",
                        evidence_refs=[mission["id"]],
                        rationale=(
                            "P10a：按研究手册进入 Initial Screen，开始核对资料底座"
                            "（4 季财报、4 次电话会、最新年报、近 6 个月券商观点）。"
                        ),
                        actor_ref=automation,
                        idempotency_key=f"{mission['id']}:{company_ref}:{FIRST_STAGE}:entered",
                    )
                    entered.append({
                        "company_ref": company_ref, "mission_version_ref": mission["id"],
                        "stage_ref": FIRST_STAGE, "status": record.get("status", "recorded"),
                    })
                    state.setdefault(company_ref, {}).setdefault(FIRST_STAGE, []).append("entered")
                except CoverageMissionError as exc:
                    skipped.append({
                        "company_ref": company_ref, "mission_version_ref": mission["id"],
                        "reason": f"{type(exc).__name__}: {exc}",
                    })
            companies = evaluate_mission(
                self.missions.connection, mission,
                planned_specs=self.planned_specs, stage_state=state,
            )
            missions.append({
                "mission_ref": mission["mission_ref"], "mission_version_ref": mission["id"],
                "companies": [
                    {
                        "company_ref": company["company_ref"], "ticker": company["ticker"],
                        "stage": company["stage"], "stage_status": company["stage_status"],
                        "gaps": company["gaps"], "blocked_on": company["blocked_on"],
                    }
                    for company in companies
                ],
                "acquisition_needs": acquisition_needs(companies),
                "discovery_needs": discovery_needs(companies),
            })
        return {
            "status": "entered" if entered else "idle", "schema_version": SCHEMA_VERSION,
            "entered": entered, "skipped": skipped, "missions": missions,
        }


__all__ = [
    "FIRST_STAGE",
    "MissionStageDriver",
    "MissionStageError",
    "SOURCE_BASE_ITEMS",
    "SPEC_READING_RANK",
    "STAGE_LABELS",
    "acquisition_needs",
    "company_priority_order",
    "discovery_needs",
    "evaluate_mission",
    "planned_spec_refs",
    "planned_spec_refs_from_directory",
    "retired_claim_refs",
    "review_sort_key",
]
