"""Immutable, zero-model weekly progress when no research template exists.

This authority observes research records; it creates neither a stance nor a
research evidence pack. Its tables share the weekly writer's authorization and
scheduled slot exclusion, while the research tables keep their original FKs.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from datetime import timedelta
from typing import Any, Mapping

from .store import canonical_json, content_hash
from .weekly_brief import (
    WeeklyBriefConflict, WeeklyBriefNotFound, WeeklyBriefValidationError,
    _canonical_record, _record, _text, _hash, _time, _table_exists, _now,
)

ISSUE_PREFIX = "weekly-brief-progress-version:"
BRIEF_KIND = "mission_progress"
# A resource bound, not a company/financial rule. Truncated collections are
# explicitly unavailable rather than mislabeled as a complete zero/count.
MAX_ROWS = 10000
MAX_COMPANIES = 256
_SOURCES = {
    "claims": ("claim_versions", "claim_version_id", "claim_json", "subject_ref"),
    "discoveries": ("coverage_mission_source_discoveries", "record_id", "record_json", "company_ref"),
    "stages": ("coverage_mission_stage_records", "record_id", "record_json", "company_ref"),
    "dossiers": ("company_dossier_versions", "version_id", "record_json", "company_ref"),
    "models": ("forecast_model_versions", "version_id", "record_json", "company_ref"),
}


_SNAPSHOT_FIELDS = {"schema_version", "snapshot_kind", "mission_version_ref", "mission_version_hash", "mission_ref", "title", "objective", "industry_ref", "universe", "period", "sources", "model_cell_reconciliations", "research_followups", "material_requests", "material_gaps", "content_hash"}
_ISSUE_FIELDS = {"schema_version", "brief_kind", "id", "brief_ref", "version", "prior_version_ref", "industry_ref", "period", "snapshot", "snapshot_hash", "body", "markdown_hash", "actor_ref", "created_at", "content_hash"}
_ADMISSION_FIELDS = {"schema_version", "brief_kind", "id", "plan_ref", "plan_hash", "plan", "policy_version_ref", "policy_version_hash", "scheduled_for", "period", "brief_ref", "issue_version_ref", "destination_ref", "actor_ref", "created_at", "content_hash"}


def _closed(record: Mapping[str, Any], fields: set[str], kind: str) -> None:
    if set(record) != fields or record.get("schema_version") != "0.1":
        raise WeeklyBriefConflict(f"{kind} has an invalid closed shape")



def template_missing(connection: sqlite3.Connection, schedule: Any, *, scheduled_for: str | None = None) -> bool:
    """Absence in this mission's scope, never a repair for a wrong pack ref.

    Other industries or disjoint company universes remain independent. A pack
    in the same industry with overlapping coverage is relevant even when this
    plan mistypes its ref; a broken record cannot establish safe absence.
    """
    if schedule.mission_progress is None or schedule.evidence_refresh is None:
        return False
    ref = schedule.evidence_refresh.evidence_pack_ref
    if any(connection.execute(
        f"SELECT 1 FROM {table} WHERE evidence_pack_ref=? LIMIT 1", (ref,),
    ).fetchone() for table in (
        "industry_evidence_pack_versions", "industry_evidence_pack_pointer",
    )):
        return False
    mission = (_mission(connection, schedule.mission_progress, scheduled_for)
               if scheduled_for is not None else _reader(connection)._active_mission_record(schedule.mission_progress))
    companies = {m["company_ref"] for m in mission["universe"]}
    rows = connection.execute(
        "SELECT record_json,content_hash FROM industry_evidence_pack_versions "
        "WHERE industry_ref=? LIMIT ?", (mission["industry_ref"], MAX_ROWS + 1),
    ).fetchall()
    if len(rows) > MAX_ROWS:
        raise WeeklyBriefConflict("cannot prove missing template within the bounded scope scan")
    for row in rows:
        record = _canonical_record(row["record_json"], "existing scoped research template")
        if record.get("content_hash") != row["content_hash"]:
            raise WeeklyBriefConflict("existing scoped research template hash drifted")
        universe = record.get("coverage_universe")
        if not isinstance(universe, list) or any(not isinstance(m, dict) or not m.get("company_ref") for m in universe):
            raise WeeklyBriefConflict("cannot prove absent template from a malformed existing scope")
        if companies.intersection(m["company_ref"] for m in universe):
            raise WeeklyBriefConflict("plan pack reference differs from an existing template in this research scope")
    return True


def _reader(connection: sqlite3.Connection) -> Any:
    from .coverage_mission import CoverageMissionAuthority
    reader = object.__new__(CoverageMissionAuthority)
    reader.connection = connection
    return reader


def _mission(connection: sqlite3.Connection, mission_ref: str, end: str) -> dict[str, Any]:
    cutoff = _time(end, "period.end")[1]
    rows = connection.execute(
        "SELECT mission_version_id,created_at FROM coverage_mission_versions "
        "WHERE mission_ref=? AND julianday(created_at)<=julianday(?) "
        "ORDER BY version_number DESC LIMIT 65", (mission_ref, end),
    ).fetchall()
    for row in rows[:64]:
        if _time(row["created_at"], "mission.created_at")[1] <= cutoff:
            return _reader(connection)._mission_record(row["mission_version_id"])
    raise WeeklyBriefConflict("no exact mission version exists at the reporting cutoff")


def _source_rows(connection: sqlite3.Connection, kind: str, companies: list[str],
                 mission_ref: str, start: str, end: str) -> dict[str, Any]:
    table, key, json_key, subject_key = _SOURCES[kind]
    if not _table_exists(connection, table):
        return {"available": False, "reason": "authority_not_initialized", "entries": []}
    marks = ",".join("?" for _ in companies)
    subject_sql = ("json_extract(claim_json,'$.subject_ref')" if kind == "claims" else "s.company_ref")
    # Products show their actual latest version at cutoff. Events/claims are
    # the bounded window, using numeric time then exact Python microseconds.
    since = "" if kind in {"dossiers", "models"} else " AND julianday(s.created_at)>=julianday(?)"
    args: list[Any] = [*companies, end]
    if since:
        args.append(start)
    mission_sql = ""
    if kind in {"discoveries", "stages"}:
        mission_sql = " AND s.mission_version_ref IN (SELECT mission_version_id FROM coverage_mission_versions WHERE mission_ref=?)"
        args.append(mission_ref)
    ordering = "s.version_number DESC,julianday(s.created_at) DESC" if kind in {"dossiers", "models"} else "julianday(s.created_at) DESC"
    rows = connection.execute(
        f"SELECT s.* FROM {table} s WHERE {subject_sql} IN ({marks}) "
        f"AND julianday(s.created_at)<=julianday(?){since}{mission_sql} "
        f"ORDER BY {ordering},s.{key} DESC LIMIT ?", (*args, MAX_ROWS + 1),
    ).fetchall()
    complete = len(rows) <= MAX_ROWS
    start_time, end_time = _time(start, "start")[1], _time(end, "end")[1]
    entries = []
    seen = set()
    mission_cache: dict[str, Any] = {}
    for row in rows[:MAX_ROWS]:
        created = _time(row["created_at"], "source.created_at")[1]
        if created > end_time or (kind not in {"dossiers", "models"} and created <= start_time):
            continue
        record = _canonical_record(row[json_key], kind)
        if (record.get("content_hash") != row["content_hash"] or record.get("id") != row[key]
            or record.get("created_at") != row["created_at"]):
            raise WeeklyBriefConflict(f"progress {kind} source identity/hash drifted")
        company = record.get(subject_key)
        if company not in companies or (kind != "claims" and row["company_ref"] != company):
            raise WeeklyBriefConflict("progress source company binding drifted")
        if kind in {"discoveries", "stages"}:
            parent_ref = record.get("mission_version_ref")
            if parent_ref not in mission_cache:
                mission_cache[parent_ref] = _reader(connection)._mission_record(parent_ref)
            parent = mission_cache[parent_ref]
            if (parent_ref != row["mission_version_ref"] or parent["mission_ref"] != mission_ref
                or parent["content_hash"] != record.get("mission_version_hash")
                or company not in {m["company_ref"] for m in parent["universe"]}):
                raise WeeklyBriefConflict("progress source mission scope/hash differs")
        if "version_number" in row.keys() and record.get("version") != row["version_number"]:
            raise WeeklyBriefConflict("progress source version identity differs")
        if kind in {"dossiers", "models"}:
            if company in seen:
                continue
            seen.add(company)
        entries.append({
            "ref": row[key], "hash": row["content_hash"], "company_ref": company,
            "created_at": row["created_at"], "record_sha256": hashlib.sha256(row[json_key].encode()).hexdigest(),
        })
    return {"available": complete, "reason": None if complete else "bounded_scan_incomplete",
            "entries": sorted(entries, key=lambda e: (e["created_at"], e["ref"]))}



def _material_requests(state_dir: Path | None, companies: list[str], industry_ref: str,
                       end: str) -> dict[str, Any]:
    from .material_requests import _reader as ledger_reader
    if state_dir is None:
        return {"available": False, "reason": "authority_not_initialized", "entries": []}
    with ledger_reader(state_dir) as c:
        if c is None or not _table_exists(c, "material_requests"):
            return {"available": False, "reason": "authority_not_initialized", "entries": []}
        c.execute("PRAGMA query_only=ON")
        c.execute("BEGIN")
        subjects = [*companies, industry_ref]
        marks = ",".join("?" for _ in subjects)
        rows = c.execute(f"SELECT * FROM material_requests WHERE subject_ref IN ({marks}) "
                         "AND julianday(created_at)<=julianday(?) ORDER BY created_at,request_id LIMIT ?",
                         (*subjects, end, MAX_ROWS + 1)).fetchall()
        if len(rows) > MAX_ROWS:
            return {"available": False, "reason": "bounded_scan_incomplete", "entries": []}
        entries = []
        event_count = 0
        cutoff = _time(end, "end")[1]
        for row in rows:
            if _time(row["created_at"], "request.created_at")[1] > cutoff:
                continue
            record = _canonical_record(row["record_json"], "material request")
            if (record["id"] != row["request_id"] or record["content_hash"] != row["content_hash"]
                or record["subject"] != row["subject_ref"] or record["created_at"] != row["created_at"]):
                raise WeeklyBriefConflict("progress material request identity/hash differs")
            events = c.execute("SELECT * FROM material_request_events WHERE request_id=? "
                               "AND julianday(created_at)<=julianday(?) ORDER BY event_id LIMIT ?",
                               (row["request_id"], end, MAX_ROWS - event_count + 1)).fetchall()
            event_count += len(events)
            if event_count > MAX_ROWS:
                return {"available": False, "reason": "bounded_scan_incomplete", "entries": []}
            events = [dict(e) for e in events if _time(e["created_at"], "event.created_at")[1] <= cutoff]
            status = events[-1]["status"] if events else "open"
            entries.append({"request": record, "events": events, "status": status,
                            "record_sha256": hashlib.sha256(row["record_json"].encode()).hexdigest(),
                            "events_hash": content_hash(events)})
        return {"available": True, "reason": None, "entries": entries}


def _replay_material_requests(state_dir: Path | None, material: Mapping[str, Any],
                              subjects: list[str], end: str) -> None:
    if not material["entries"]:
        return
    from .material_requests import _reader as ledger_reader
    if state_dir is None:
        raise WeeklyBriefConflict("progress material request ledger is missing")
    with ledger_reader(state_dir) as c:
        if c is None:
            raise WeeklyBriefConflict("progress material request ledger is missing")
        c.execute("PRAGMA query_only=ON")
        for entry in material["entries"]:
            record = entry["request"]
            row = c.execute("SELECT * FROM material_requests WHERE request_id=?", (record["id"],)).fetchone()
            if (row is None or row["content_hash"] != record["content_hash"]
                or row["subject_ref"] not in subjects or record["subject"] != row["subject_ref"]
                or _time(record["created_at"], "request.created_at")[1] > _time(end, "end")[1]
                or hashlib.sha256(row["record_json"].encode()).hexdigest() != entry["record_sha256"]
                or _canonical_record(row["record_json"], "material request") != record
                or content_hash(entry["events"]) != entry["events_hash"]):
                raise WeeklyBriefConflict("progress material request proof/scope differs")
            for event in entry["events"]:
                current = c.execute("SELECT * FROM material_request_events WHERE event_id=?", (event["event_id"],)).fetchone()
                if (current is None or dict(current) != event or event["request_id"] != record["id"]
                    or _time(event["created_at"], "event.created_at")[1] > _time(end, "end")[1]):
                    raise WeeklyBriefConflict("progress material event proof differs")
            status = entry["events"][-1]["status"] if entry["events"] else "open"
            if status != entry["status"]:
                raise WeeklyBriefConflict("progress material status differs")


def _cell_display(connection: sqlite3.Connection, entry: Mapping[str, Any]) -> dict[str, Any]:
    from .model_cell_reconciliation import read_existing
    from .model_forecast_driver import ForecastModelAuthority
    from .financial_display import model_line_display
    record = next((r for r in read_existing(connection, entry["company_ref"]) if r["id"] == entry["ref"]), None)
    if record is None or record["content_hash"] != entry["hash"]:
        raise WeeklyBriefConflict("progress reconciliation source differs")
    reader = object.__new__(ForecastModelAuthority)
    reader.connection = connection
    model = reader.model(record["model_version_ref"])
    if model["company_ref"] != entry["company_ref"] or model["content_hash"] != record["model_hash"]:
        raise WeeklyBriefConflict("progress reconciliation model scope/hash differs")
    result = next((r for r in model["results"] if r["ref"] == record["result_ref"]), None)
    if result is None:
        raise WeeklyBriefConflict("progress reconciliation result is absent from its bound model")
    line = next((r for r in (model.get("financial_statement_structure") or {}).get("lines") or []
                 if r["ref"] == str(record["result_ref"]).removeprefix("result:")), {})
    return {"model_hash": model["content_hash"], "result_ref": result["ref"],
            "label": model_line_display(result.get("label"), line.get("concept"))}

def capture_snapshot(connection: sqlite3.Connection, schedule: Any,
                     start: str, end: str, *, state_dir: Path | None = None) -> dict[str, Any]:
    mission = _mission(connection, schedule.mission_progress, end)
    universe = mission["universe"]
    if not 0 < len(universe) <= MAX_COMPANIES:
        raise WeeklyBriefValidationError("progress universe exceeds the bounded resource limit")
    companies = [m["company_ref"] for m in universe]
    sources = {kind: _source_rows(connection, kind, companies, mission["mission_ref"], start, end)
               for kind in _SOURCES}
    from .model_cell_reconciliation import brief_entries as cells
    from .research_followup import brief_entries as followups
    reconciliations = cells(connection, companies, _time(start, "start")[1], _time(end, "end")[1])
    if reconciliations is not None:
        reconciliations = [{**entry, "display": _cell_display(connection, entry)} for entry in reconciliations]
    followup_entries = followups(connection, companies, as_of=end)
    if len(reconciliations or []) > MAX_ROWS or len(followup_entries or []) > MAX_ROWS:
        raise WeeklyBriefValidationError("progress reconciliation/followup resource limit exceeded")
    return _record({
        "schema_version": "0.1", "snapshot_kind": BRIEF_KIND,
        "mission_version_ref": mission["id"], "mission_version_hash": mission["content_hash"],
        "mission_ref": mission["mission_ref"], "title": mission["title"],
        "objective": mission["objective"], "industry_ref": mission["industry_ref"],
        "universe": universe, "period": {"start": start, "end": end},
        "sources": sources, "model_cell_reconciliations": reconciliations,
        "research_followups": followup_entries,
        "material_requests": _material_requests(state_dir, companies, mission["industry_ref"], end),
        "material_gaps": [{"kind": "research_template_missing", "ref": schedule.evidence_refresh.evidence_pack_ref,
                           "label": "行业研究证据包尚待建立。"}],
    })


def replay_snapshot(connection: sqlite3.Connection, snapshot: Mapping[str, Any], *, state_dir: Path | None = None) -> None:
    _closed(snapshot, _SNAPSHOT_FIELDS, "progress snapshot")
    base = dict(snapshot)
    declared = base.pop("content_hash", None)
    if content_hash(base) != declared or snapshot.get("snapshot_kind") != BRIEF_KIND:
        raise WeeklyBriefConflict("progress snapshot hash/type drifted")
    mission = _reader(connection)._mission_record(snapshot["mission_version_ref"])
    if (mission["content_hash"] != snapshot["mission_version_hash"]
        or any(mission[k] != snapshot[k] for k in ("mission_ref", "universe", "industry_ref", "title", "objective"))):
        raise WeeklyBriefConflict("progress mission scope/hash drifted")
    companies = [m["company_ref"] for m in snapshot["universe"]]
    start, end = (_time(snapshot["period"][k], k)[1] for k in ("start", "end"))
    if _time(mission["created_at"], "mission.created_at")[1] > end or end <= start:
        raise WeeklyBriefConflict("progress mission/window binding differs")
    if set(snapshot["sources"]) != set(_SOURCES):
        raise WeeklyBriefConflict("progress sources have an invalid closed shape")
    for kind, collection in snapshot["sources"].items():
        table, key, json_key, subject_key = _SOURCES[kind]
        if (set(collection) != {"available", "reason", "entries"} or type(collection["available"]) is not bool
            or not isinstance(collection["entries"], list) or len(collection["entries"]) > MAX_ROWS):
            raise WeeklyBriefConflict("progress source collection has an invalid closed shape")
        for entry in collection["entries"]:
            if set(entry) != {"ref", "hash", "company_ref", "created_at", "record_sha256"}:
                raise WeeklyBriefConflict("progress source proof has an invalid closed shape")
            row = connection.execute(f"SELECT * FROM {table} WHERE {key}=?", (entry["ref"],)).fetchone()
            if row is None or row["content_hash"] != entry["hash"] or row["created_at"] != entry["created_at"]:
                raise WeeklyBriefConflict("progress source no longer replays")
            record = _canonical_record(row[json_key], kind)
            if (hashlib.sha256(row[json_key].encode()).hexdigest() != entry["record_sha256"]
                or record.get("id") != entry["ref"] or record.get("content_hash") != entry["hash"]
                or record.get("created_at") != entry["created_at"]
                or record.get(subject_key) != entry["company_ref"] or entry["company_ref"] not in companies):
                raise WeeklyBriefConflict("progress source proof/scope differs")
            created = _time(entry["created_at"], "source.created_at")[1]
            if created > end or (kind not in {"dossiers", "models"} and created <= start):
                raise WeeklyBriefConflict("progress source is outside its frozen reporting window")
            if "version_number" in row.keys() and record.get("version") != row["version_number"]:
                raise WeeklyBriefConflict("progress source version identity differs")
            if kind in {"discoveries", "stages"}:
                parent = _reader(connection)._mission_record(record.get("mission_version_ref"))
                if (row["mission_version_ref"] != parent["id"] or parent["mission_ref"] != snapshot["mission_ref"]
                    or parent["content_hash"] != record.get("mission_version_hash")
                    or entry["company_ref"] not in {m["company_ref"] for m in parent["universe"]}):
                    raise WeeklyBriefConflict("progress source mission scope/hash differs")
    _replay_material_requests(state_dir, snapshot["material_requests"], [*companies, snapshot["industry_ref"]], snapshot["period"]["end"])
    from .model_cell_reconciliation import replay_brief_entries as replay_cells
    from .research_followup import replay_brief_entries as replay_followups
    if snapshot["model_cell_reconciliations"] is not None:
        replay_cells(connection, snapshot["model_cell_reconciliations"])
        for entry in snapshot["model_cell_reconciliations"]:
            if entry["display"] != _cell_display(connection, entry):
                raise WeeklyBriefConflict("progress reconciliation display proof differs")
    if snapshot["research_followups"] is not None:
        replay_followups(connection, snapshot["research_followups"], companies, as_of=snapshot["period"]["end"])


def _body(snapshot: Mapping[str, Any]) -> str:
    start_label = _time(snapshot["period"]["start"], "period.start")[1].strftime("%Y-%m-%d %H:%M")
    end_label = _time(snapshot["period"]["end"], "period.end")[1].strftime("%Y-%m-%d %H:%M")
    lines = ["# 研究进展周报", "", snapshot["title"], "",
             f"报告窗口：{start_label} 至 {end_label}（UTC）", "",
             "## 研究目标", "", snapshot["objective"], "",
             "本期汇总当前项目的研究进展、资料覆盖情况与待处理事项。", "",
             "## 本期进展", "", "| 公司 | 新增事实版本记录 | 资料发现记录 | 研究处理记录 | 公司档案 | 预测模型 |",
             "| --- | ---: | ---: | ---: | --- | --- |"]
    for member in snapshot["universe"]:
        company = member["company_ref"]
        values = []
        for kind in _SOURCES:
            source = snapshot["sources"][kind]
            entries = [e for e in source["entries"] if e["company_ref"] == company]
            if not source["available"]:
                values.append("暂无完整记录")
            elif kind in {"dossiers", "models"}:
                values.append("已登记" if entries else "尚未形成")
            else:
                values.append(str(len(entries)))
        lines.append(f"| {member['ticker']} | " + " | ".join(values) + " |")
    lines += ["", "## 资料缺项", ""]
    lines += [f"- {gap['label']}" for gap in snapshot["material_gaps"]]
    material = snapshot["material_requests"]
    if not material["available"]:
        lines.append("资料需求记录尚待建立。")
    elif not material["entries"]:
        lines.append("本期尚无资料需求登记。")
    else:
        from .material_requests import STATUS_LABELS
        for entry in material["entries"]:
            request = entry["request"]
            need = request["need"]
            description = need.get("question") or need.get("data_item") or "、".join(need.get("doc_kinds") or [])
            ticker = next((m["ticker"] for m in snapshot["universe"] if m["company_ref"] == request["subject"]), "行业范围")
            lines.append(f"- {ticker}：{description}；处理状态：{STATUS_LABELS[entry['status']]}；资料需求期限：{request['deadline']}。")
    product_lines = []
    for member in snapshot["universe"]:
        company = member["company_ref"]
        for kind, label in (("dossiers", "公司档案"), ("models", "预测模型")):
            if snapshot["sources"][kind]["available"] and not any(e["company_ref"] == company for e in snapshot["sources"][kind]["entries"]):
                product_lines.append(f"- {member['ticker']}：{label}尚未形成。")
    if product_lines:
        lines += ["", "## 研究产品状态", "", *product_lines]
    tickers = {m["company_ref"]: m["ticker"] for m in snapshot["universe"]}
    lines += ["", "## 预测对账", ""]
    cells = snapshot["model_cell_reconciliations"]
    if cells is None:
        lines.append("预测对账记录尚待建立。")
    elif not cells:
        lines.append("本期没有新增预测对账记录。")
    else:
        lines += ["| 公司 | 预测单元 | 期间 | 状态 | 偏差 | 原因 |", "| --- | --- | --- | --- | --- | --- |"]
        labels = {"compared": "已对账", "unavailable": "暂不能对账"}
        reasons = {
            "no_declared_comparable_filed_concept": "尚未明确可比较的申报科目",
            "exact_period_identity_unit_actual_missing": "缺少期间、口径与单位一致的实际值",
            "forecast_not_known_before_actual_publication": "该预测形成于实际结果公布之后，不能作为事前预测对账",
            "conflicting_exact_actual_sources": "同一口径的实际值来源存在冲突",
            "forecast_cell_unavailable": "该预测单元尚无可用数值",
            None: "期间、口径与单位一致",
        }
        for cell in cells:
            deviation = cell.get("deviation_percent")
            deviation_label = "未有可比数值" if deviation is None else f"{deviation}%"
            period = cell["period"]
            period_label = f"{period['start']} 至 {period['end']}"
            lines.append(f"| {tickers.get(cell['company_ref'], cell['company_ref'])} | {cell['display']['label']} | {period_label} | {labels.get(cell['status'], '状态待核对')} | {deviation_label} | {reasons.get(cell['reason'], '原因待核对，见正式记录')} |")
    lines += ["", "## 后续研究事项", ""]
    followups = snapshot["research_followups"]
    if followups is None:
        lines.append("尚无可复核的后续研究事项登记。")
    elif not followups:
        lines.append("截至报告窗口结束，没有适用于本研究范围的已登记后续事项。")
    else:
        states = {"open": "待开展", "unassigned": "待明确负责人", "completed": "已完成", "no_revision_needed": "无需修订", "declined": "不开展", "withdrawn": "已撤回"}
        for followup in followups:
            title = followup.get("title") or followup.get("reason") or "后续研究事项"
            owner = followup.get("owner_ref") or "尚未明确"
            due = followup.get("due_on") or "尚未明确"
            state = states.get(followup.get("state"), "状态待核对")
            terminal = followup.get("terminal_decision") or {}
            evidence = terminal.get("closure_evidence") or []
            closure = "；".join(f"{e['ref']}（hash={e['hash']}）" for e in evidence) or "尚无关闭证据"
            lines.append(f"- {tickers.get(followup.get('company_ref'), '研究范围')}：{title}；负责人：{owner}；期限：{due}；状态：{state}；关闭依据：{closure}。")
    lines += ["", "## 附录：版本与出处", "", f"研究目标版本：{snapshot['mission_version_ref']}｜hash={snapshot['mission_version_hash']}"]
    for kind, source in snapshot["sources"].items():
        if not source["available"]:
            lines.append(f"- {kind}：记录尚不完整（{source['reason']}），未按零计。")
        for entry in source["entries"]:
            lines.append(f"- {entry['ref']}｜hash={entry['hash']}")
    for entry in material["entries"]:
        request = entry["request"]
        lines.append(f"- {request['id']}｜hash={request['content_hash']}｜events={entry['events_hash']}")
    for entry in (cells or []) + (followups or []):
        lines.append(f"- {entry.get('ref') or entry.get('id')}｜hash={entry.get('hash') or entry.get('content_hash')}")
    return "\n".join(lines).rstrip() + "\n"


class MissionProgressWeeklyAuthority:
    def __init__(self, parent: Any):
        self.parent = parent
        self.connection = parent.connection
        self.state_dir = None if parent.store.path == ":memory:" else Path(parent.store.path).parent

    def admit(self, *, schedule: Any, active: Mapping[str, Any], cycle_id: str,
              issue_id: str, scheduled_for: str, period_start: str, as_of: str,
              actor_ref: str, idempotency_key: str) -> dict[str, Any]:
        from .weekly_brief_coordinator import _validate_policy, _utc
        if actor_ref != "core":
            raise WeeklyBriefValidationError("progress admission requires the core actor")
        _validate_policy(active, schedule, _time(as_of, "as_of")[1])
        if not schedule.destination_ref.startswith("cockpit:weekly-brief:"):
            raise WeeklyBriefValidationError("mission progress may only be delivered locally")
        if not issue_id.startswith(ISSUE_PREFIX) or not template_missing(self.connection, schedule, scheduled_for=scheduled_for):
            raise WeeklyBriefConflict("progress requires a genuinely uninitialized research template")
        now = _time(as_of, "as_of")[1]
        due = schedule.latest_due(now)
        start, end = _time(period_start, "period_start")[1], _time(scheduled_for, "scheduled_for")[1]
        expected_digest = content_hash({"plan_ref": schedule.plan_ref, "plan_hash": schedule.content_hash,
                                        "scheduled_for": scheduled_for})[:32]
        if (due != end or scheduled_for != _utc(due) or period_start != _utc(due - timedelta(days=7))
            or end - start != timedelta(days=7)
            or cycle_id != f"weekly-brief-cycle:{expected_digest}" or issue_id != f"{ISSUE_PREFIX}{expected_digest}"):
            raise WeeklyBriefConflict("progress cycle is outside the exact authorized schedule/window")
        snapshot = capture_snapshot(self.connection, schedule, period_start, scheduled_for, state_dir=self.state_dir)
        request = {"plan": schedule.to_dict(), "cycle_id": cycle_id, "issue_id": issue_id,
                   "scheduled_for": scheduled_for, "period_start": period_start,
                   "policy_version_ref": active["policy_version_id"], "policy_version_hash": active["content_hash"]}
        request_hash = self.parent._request_hash("admit_mission_progress_weekly", request)
        created_at = _now()
        with self.parent._transaction() as cur:
            duplicate = self.parent._idem(cur, idempotency_key, "admit_mission_progress_weekly", request_hash)
            if duplicate is not None:
                return duplicate
            current = self.parent.store.active_policy()
            if current["policy_version_id"] != active["policy_version_id"] or current["content_hash"] != active["content_hash"]:
                raise WeeklyBriefConflict("progress admission policy changed")
            _validate_policy(current, schedule, _time(as_of, "as_of")[1])
            if not template_missing(self.connection, schedule, scheduled_for=scheduled_for):
                raise WeeklyBriefConflict("research template arrived before progress admission")
            replay_snapshot(self.connection, snapshot, state_dir=self.state_dir)
            prior = cur.execute("SELECT version_id,version_number FROM weekly_brief_progress_issues WHERE brief_ref=? ORDER BY version_number DESC LIMIT 1", (schedule.brief_ref,)).fetchone()
            body = _body(snapshot)
            issue = _record({
                "schema_version": "0.1", "brief_kind": BRIEF_KIND, "id": issue_id,
                "brief_ref": schedule.brief_ref, "version": 1 if prior is None else prior["version_number"] + 1,
                "prior_version_ref": None if prior is None else prior["version_id"],
                "industry_ref": snapshot["industry_ref"], "period": snapshot["period"],
                "snapshot": snapshot, "snapshot_hash": snapshot["content_hash"],
                "body": body, "markdown_hash": hashlib.sha256(body.encode()).hexdigest(),
                "actor_ref": actor_ref, "created_at": created_at,
            })
            admission = _record({
                "schema_version": "0.1", "brief_kind": BRIEF_KIND, "id": cycle_id,
                "plan_ref": schedule.plan_ref, "plan_hash": schedule.content_hash, "plan": schedule.to_dict(),
                "policy_version_ref": active["policy_version_id"], "policy_version_hash": active["content_hash"],
                "scheduled_for": scheduled_for, "period": snapshot["period"], "brief_ref": schedule.brief_ref,
                "issue_version_ref": issue_id, "destination_ref": schedule.destination_ref,
                "actor_ref": actor_ref, "created_at": created_at,
            })
            try:
                cur.execute("INSERT INTO weekly_brief_progress_issues VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                    issue_id, issue["brief_ref"], issue["version"], issue["prior_version_ref"], issue["industry_ref"],
                    issue["snapshot_hash"], issue["markdown_hash"], period_start, scheduled_for,
                    canonical_json(issue), issue["content_hash"], actor_ref, created_at,
                ))
            except sqlite3.IntegrityError as exc:
                raise WeeklyBriefConflict("progress issue conflicts with immutable authority") from exc
            cur.execute("INSERT INTO weekly_brief_progress_cycle_admissions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                cycle_id, schedule.plan_ref, schedule.content_hash, active["policy_version_id"], active["content_hash"],
                scheduled_for, period_start, scheduled_for, schedule.brief_ref, issue_id, schedule.destination_ref,
                canonical_json(admission), admission["content_hash"], actor_ref, created_at,
            ))
            result = {"status": "fresh", **admission}
            self.parent._save_idem(cur, idempotency_key, "admit_mission_progress_weekly", request_hash, result, created_at)
            return result

    def cycle_admission(self, cycle_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM weekly_brief_progress_cycle_admissions WHERE cycle_id=?", (cycle_id,)).fetchone()
        if row is None:
            raise WeeklyBriefNotFound("weekly progress cycle was not found")
        record = _canonical_record(row["record_json"], "progress admission")
        _closed(record, _ADMISSION_FIELDS, "progress admission")
        if record["brief_kind"] != BRIEF_KIND or record["actor_ref"] != "core":
            raise WeeklyBriefConflict("progress admission type/actor differs")
        for field in ("plan_ref", "plan_hash", "policy_version_ref", "policy_version_hash", "scheduled_for", "brief_ref", "issue_version_ref", "destination_ref", "actor_ref", "created_at", "content_hash"):
            if record.get(field) != row[field]:
                raise WeeklyBriefConflict("progress admission column/hash drifted")
        if record.get("id") != row["cycle_id"] or record.get("period") != {"start": row["period_start"], "end": row["period_end"]}:
            raise WeeklyBriefConflict("progress admission identity/window drifted")
        from .weekly_brief_coordinator import WeeklyBriefSchedulePlan
        plan = WeeklyBriefSchedulePlan.from_mapping(record["plan"])
        if (plan.content_hash != record["plan_hash"] or plan.plan_ref != record["plan_ref"]
            or plan.brief_ref != record["brief_ref"] or plan.destination_ref != record["destination_ref"]):
            raise WeeklyBriefConflict("progress admission plan binding differs")
        policy = self.connection.execute("SELECT content_hash FROM governance_policy_versions WHERE policy_version_id=?", (record["policy_version_ref"],)).fetchone()
        if policy is None or policy["content_hash"] != record["policy_version_hash"]:
            raise WeeklyBriefConflict("progress admission policy authority drifted")
        return record

    def issue(self, version_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM weekly_brief_progress_issues WHERE version_id=?", (version_id,)).fetchone()
        if row is None:
            raise WeeklyBriefNotFound("weekly progress issue was not found")
        record = _canonical_record(row["record_json"], "progress issue")
        _closed(record, _ISSUE_FIELDS, "progress issue")
        if record["actor_ref"] != "core":
            raise WeeklyBriefConflict("progress issue actor differs")
        for field in ("brief_ref", "industry_ref", "snapshot_hash", "markdown_hash", "content_hash", "actor_ref", "created_at"):
            if record.get(field) != row[field]:
                raise WeeklyBriefConflict("progress issue column/hash drifted")
        if (record.get("id") != row["version_id"] or record.get("version") != row["version_number"]
            or record.get("prior_version_ref") != row["prior_version_id"] or record.get("brief_kind") != BRIEF_KIND
            or record.get("period") != {"start": row["period_start"], "end": row["period_end"]}
            or record["snapshot"]["content_hash"] != record["snapshot_hash"]
            or record["snapshot"]["period"] != record["period"] or record["snapshot"]["industry_ref"] != record["industry_ref"]
            or hashlib.sha256(record["body"].encode()).hexdigest() != record["markdown_hash"]):
            raise WeeklyBriefConflict("progress issue frozen input/artifact binding differs")
        replay_snapshot(self.connection, record["snapshot"], state_dir=self.state_dir)
        return record

    def render_markdown(self, version_id: str) -> dict[str, Any]:
        issue = self.issue(version_id)
        return _record({"schema_version": "0.1", "projection_kind": "weekly_brief_markdown",
                        "brief_kind": BRIEF_KIND, "issue_version_ref": issue["id"],
                        "issue_version_hash": issue["content_hash"], "snapshot_hash": issue["snapshot_hash"],
                        "media_type": "text/markdown", "body": issue["body"]})

    def record_delivery(self, *, cycle_id: str, issue_version_ref: str, issue_version_hash: str,
                        destination_ref: str, external_message_ref: str, artifact_sha256: str,
                        delivered_at: str, delivery_id: str, actor_ref: str, idempotency_key: str) -> dict[str, Any]:
        if actor_ref != "core":
            raise WeeklyBriefValidationError("progress delivery requires the core actor")
        admission = self.cycle_admission(_text(cycle_id, "cycle_id"))
        issue = self.issue(_text(issue_version_ref, "issue_version_ref"))
        if (admission["issue_version_ref"] != issue_version_ref or admission["destination_ref"] != destination_ref
            or issue["content_hash"] != _hash(issue_version_hash, "issue_version_hash")
            or issue["markdown_hash"] != _hash(artifact_sha256, "artifact_sha256")):
            raise WeeklyBriefConflict("progress delivery is outside its admitted exact artifact/destination")
        delivered_at, delivered_time = _time(delivered_at, "delivered_at")
        if delivered_time < _time(issue["created_at"], "issue.created_at")[1]:
            raise WeeklyBriefConflict("progress cannot be delivered before it was created")
        wire = _record({"schema_version": "0.1", "brief_kind": BRIEF_KIND,
                        "id": _text(delivery_id, "delivery_id"), "issue_version_ref": issue_version_ref,
                        "issue_version_hash": issue_version_hash, "destination_ref": destination_ref,
                        "external_message_ref": _text(external_message_ref, "external_message_ref"),
                        "artifact_sha256": artifact_sha256, "delivered_at": delivered_at, "actor_ref": actor_ref})
        operation = "record_scheduled_mission_progress_delivery"
        request_hash = self.parent._request_hash(operation, wire)
        with self.parent._transaction() as cur:
            duplicate = self.parent._idem(cur, _text(idempotency_key, "idempotency_key"), operation, request_hash)
            if duplicate is not None:
                return duplicate
            try:
                cur.execute("INSERT INTO weekly_brief_progress_deliveries VALUES(?,?,?,?,?,?,?,?,?,?)", (
                    wire["id"], issue_version_ref, issue_version_hash, destination_ref, external_message_ref,
                    artifact_sha256, canonical_json(wire), wire["content_hash"], actor_ref, delivered_at,
                ))
            except sqlite3.IntegrityError as exc:
                raise WeeklyBriefConflict("progress delivery already exists") from exc
            result = {"status": "fresh", **wire}
            self.parent._save_idem(cur, idempotency_key, operation, request_hash, result, delivered_at)
            return result
