"""Cockpit v2 plane (P9d-18, ADR-0006): goal, steer, log, ask, approve.

The owner's cockpit has five places and no machine language:

- **goal**: the standing research goal (the active CoverageMission), the
  sub-tasks the system derived from it (one lane per company, one per
  source), and how far each has come;
- **steer**: a sentence of direction, translated into a concrete change to
  the mission's research questions, shown back in plain words, and published
  as a new mission version only when the owner confirms;
- **log**: what the system is doing, assembled from the lane tickets on
  disk, the heartbeat, and the Ledger's recent Claims and reviews;
- **ask**: an ad-hoc question answered from the formal Claims only, with the
  Claims it leaned on shown underneath;
- **approve**: every open human checkpoint (thesis admission, capability
  promotion, planner proposal, forecast overturn) with approve/reject.

The plane reads the Core read-only and never holds a Core write handle; every
write goes through the writer as the owner's Tailscale-derived human
principal, exactly as the review plane does.  Model calls go through
:mod:`cockpit_model`: routed, budgeted against the mission, replayable.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import threading
import time
from collections.abc import Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .cockpit_model import CockpitModel, CockpitModelError, unwrap_json_object
from .claim_retirement import REASON_LABELS as CLAIM_REASON_LABELS
from .mission_stage import evaluate_mission, planned_spec_refs_from_directory, retired_claim_refs
from .governance_cli import GovernanceCliError, ephemeral_call
from .store import content_hash
from .writer_protocol import RemoteError

SCHEMA_VERSION = "0.1"
LANES = ("discoveries", "fetches", "acquisitions", "extractions", "sec-lane-runs")
LANE_LABELS = {
    "discoveries": "搜索资料", "fetches": "获取网页", "acquisitions": "获取研报",
    "extractions": "阅读抽取", "sec-lane-runs": "读取财报数据",
}
COMPANY_NAMES = {
    "ACN": "Accenture", "CTSH": "Cognizant", "EPAM": "EPAM", "IBM": "IBM", "DXC": "DXC Technology",
}
SOURCE_LABELS = {
    "source:sec-edgar": "SEC 财报数据", "source:alphaengine": "卖方研报与电话会",
    "source:company-ir": "公司投资者关系", "source:guidepoint": "专家访谈", "source:web-search": "公开网页搜索",
}
# P11x: what a figure is worth, in the owner's language. A number the company
# filed is its published figure; a number said on a call is a record of the
# saying. Both are kept; the label is how the difference stays visible.
_EMPTY_FIGURES: dict = {"total": 0, "by_grade": {}, "latest": []}
FIGURE_GRADE_LABELS = {
    "company-filed-document": "公司文件披露",
    "earnings-call-transcript": "电话会口述（未经财报核对）",
}
STAGE_LABELS = {
    "industry_framework": "行业框架", "initial_screen": "初步筛选", "industry_model": "行业模型",
    "company_model": "公司模型", "forecast_lines": "预测线", "investment_memo": "投资备忘录",
    "weekly_brief": "每周简报", "deep_insight_gate": "深度洞察", "continuous_coverage": "持续覆盖",
}
JOB_TTL_SECONDS = 6 * 3600
MAX_JOBS = 200
MAX_CLAIMS_IN_PROMPT = 400
MAX_PROMPT_CHARS = 90_000


def _ticket_still_running(ticket: Mapping[str, Any]) -> bool:
    """A ticket is running only while its child actually is.

    A launcher settles a ticket when something asks about that exact ticket, so
    children killed by a restart stay ``running`` on disk until someone does.
    Live, 35 SEC lane tickets from before a reboot filled the owner's page with
    "reading SEC financials" for thirteen hours while nothing was running.
    Reporting a dead pid as busy is not a display quirk -- it is the page
    saying work is happening when none is.
    """

    if ticket.get("status") != "running":
        return False
    pid = ticket.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        # No pid recorded: nothing to check, so believe the ticket rather than
        # hide work that may be real.
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


class CockpitError(RuntimeError):
    """A cockpit request was refused; the message is safe to show."""


class CockpitConflict(CockpitError):
    """The record the owner acted on changed underneath them."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _table_exists(connection, name: str) -> bool:
    """Whether this Core has the table yet; a fresh deploy may not."""

    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,),
    ).fetchone() is not None


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _text(value: Any, name: str, *, maximum: int = 4000, minimum: int = 1) -> str:
    if not isinstance(value, str):
        raise CockpitError(f"{name} must be text")
    stripped = value.strip()
    if len(stripped) < minimum or len(stripped) > maximum:
        raise CockpitError(f"{name} must be {minimum}..{maximum} characters")
    return stripped


def _sha(value: Any, name: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise CockpitError(f"{name} must be a SHA-256 hex digest")
    return value


def _path(value: Any, name: str) -> Path:
    if not isinstance(value, str) or not value:
        raise CockpitError(f"{name} must be a non-empty path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise CockpitError(f"{name} must be absolute")
    return path


@dataclass(frozen=True, slots=True)
class CockpitConfig:
    core_db: Path
    state_dir: Path
    heartbeat_path: Path
    scheduler_db: Path
    journal_path: Path
    model_config_path: Path | None = None
    mission_ref: str | None = None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CockpitConfig":
        fields = {"core_db", "state_dir", "heartbeat_path", "scheduler_db", "journal_path",
                  "model_config_path", "mission_ref"}
        if not isinstance(raw, Mapping) or set(raw) - fields or not {"core_db", "state_dir", "heartbeat_path",
                                                                       "scheduler_db", "journal_path"} <= set(raw):
            raise CockpitError("cockpit config has an invalid shape")
        model = raw.get("model_config_path")
        mission = raw.get("mission_ref")
        if mission is not None and (not isinstance(mission, str) or not mission.startswith("coverage-mission:")):
            raise CockpitError("mission_ref must name a coverage mission")
        return cls(
            core_db=_path(raw["core_db"], "core_db"), state_dir=_path(raw["state_dir"], "state_dir"),
            heartbeat_path=_path(raw["heartbeat_path"], "heartbeat_path"),
            scheduler_db=_path(raw["scheduler_db"], "scheduler_db"),
            journal_path=_path(raw["journal_path"], "journal_path"),
            model_config_path=None if model is None else _path(model, "model_config_path"),
            mission_ref=mission,
        )


_JOURNAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS cockpit_jobs (
    job_id TEXT PRIMARY KEY, kind TEXT NOT NULL, login TEXT NOT NULL, status TEXT NOT NULL,
    request_json TEXT NOT NULL, result_json TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cockpit_drafts (
    draft_id TEXT PRIMARY KEY, kind TEXT NOT NULL, login TEXT NOT NULL, input_text TEXT NOT NULL,
    draft_json TEXT NOT NULL, content_hash TEXT NOT NULL, status TEXT NOT NULL, published_ref TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cockpit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL,
    detail TEXT, login TEXT, refs_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS cockpit_events_at ON cockpit_events(at);
"""


class CockpitJournal:
    """Owner-only SQLite next to the state: jobs, drafts, and cockpit-originated events."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None, timeout=10)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(_JOURNAL_SCHEMA)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        self._lock = threading.Lock()

    def close(self) -> None:
        self.connection.close()

    def write(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            self.connection.execute(sql, params)

    def rows(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.connection.execute(sql, params).fetchall()

    def record_event(self, *, kind: str, title: str, detail: str | None, login: str | None, refs: Mapping[str, Any]) -> None:
        self.write("INSERT INTO cockpit_events(at,kind,title,detail,login,refs_json) VALUES(?,?,?,?,?,?)",
                   (_iso(_now()), kind, title, detail, login, json.dumps(dict(refs), ensure_ascii=False, sort_keys=True)))


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


class _TicketCache:
    """Ticket and summary JSON per lane directory, re-read only when a file changes."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self._entries: dict[str, tuple[float, float, dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def tickets(self) -> list[dict[str, Any]]:
        result = []
        for lane in LANES:
            root = self.state_dir / lane
            if not root.is_dir():
                continue
            for entry in os.scandir(root):
                if not entry.is_dir():
                    continue
                ticket_path, summary_path = Path(entry.path) / "ticket.json", Path(entry.path) / "summary.json"
                try:
                    t_m = ticket_path.stat().st_mtime
                except OSError:
                    continue
                try:
                    s_m = summary_path.stat().st_mtime
                except OSError:
                    s_m = 0.0
                key = entry.path
                with self._lock:
                    cached = self._entries.get(key)
                if cached is not None and cached[0] == t_m and cached[1] == s_m:
                    result.append(cached[2])
                    continue
                ticket = _load_json(ticket_path)
                if not isinstance(ticket, dict):
                    continue
                summary = _load_json(summary_path) if s_m else None
                record = {"lane": lane, "dir": entry.name, "ticket": ticket,
                          "summary": summary if isinstance(summary, dict) else None,
                          "ticket_mtime": datetime.fromtimestamp(t_m, tz=timezone.utc).isoformat(timespec="seconds")}
                with self._lock:
                    self._entries[key] = (t_m, s_m, record)
                result.append(record)
        return result


def _host(url: str) -> str:
    match = re.match(r"https?://([^/]+)", url or "")
    return match.group(1).lower().removeprefix("www.") if match else ""


class CockpitPlane:
    _claims_cache: tuple[tuple[int, str | None, int], list[dict[str, Any]]] | None

    def __init__(self, config: CockpitConfig, *, writer_socket: Path, token_config: Path,
                 governance_call: Callable[..., Any] = ephemeral_call, model: CockpitModel | None = None,
                 model_factory: Callable[[Mapping[str, Any]], CockpitModel] | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.config = config
        self.writer_socket = writer_socket
        self.token_config = token_config
        self.governance_call = governance_call
        self.clock = clock or _now
        self.journal = CockpitJournal(config.journal_path)
        self.tickets = _TicketCache(config.state_dir)
        self._model = model
        self._model_factory = model_factory
        self._model_error: str | None = None
        self._jobs: dict[str, dict[str, Any]] = {}
        self._jobs_lock = threading.Lock()
        self._claims_cache: tuple[tuple[int, str | None], list[dict[str, Any]]] | None = None
        self._url_cache: tuple[int, dict[str, dict[str, Any]]] | None = None

    def close(self) -> None:
        self.journal.close()

    # -- Core read-only ------------------------------------------------------

    @contextmanager
    def _core(self) -> Iterator[sqlite3.Connection]:
        uri = f"file:{self.config.core_db}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA busy_timeout = 5000")
            yield connection
        finally:
            connection.close()

    def _mission(self, core: sqlite3.Connection) -> dict[str, Any]:
        if self.config.mission_ref is not None:
            row = core.execute(
                "SELECT v.record_json FROM coverage_mission_versions v JOIN coverage_mission_pointer p "
                "ON p.mission_version_id=v.mission_version_id WHERE p.mission_ref=?", (self.config.mission_ref,),
            ).fetchone()
        else:
            row = core.execute(
                "SELECT v.record_json FROM coverage_mission_versions v JOIN coverage_mission_pointer p "
                "ON p.mission_version_id=v.mission_version_id ORDER BY p.updated_at DESC LIMIT 1",
            ).fetchone()
        if row is None:
            raise CockpitError("no research goal has been published yet")
        return json.loads(row["record_json"])

    def _mission_versions(self, core: sqlite3.Connection, mission_ref: str) -> list[dict[str, Any]]:
        rows = core.execute(
            "SELECT record_json FROM coverage_mission_versions WHERE mission_ref=? ORDER BY version_number DESC",
            (mission_ref,),
        ).fetchall()
        result = []
        for row in rows:
            record = json.loads(row["record_json"])
            result.append({"version": record["version"], "id": record["id"], "created_at": record["created_at"],
                           "title": record["title"], "objective": record["objective"],
                           "research_questions": list(record["research_questions"]),
                           "universe": [m["ticker"] for m in record["universe"]]})
        return result

    def _claims(self, core: sqlite3.Connection) -> list[dict[str, Any]]:
        row = core.execute("SELECT COUNT(*) AS n, MAX(created_at) AS latest FROM claim_versions").fetchone()
        # P10b: a retired Claim never reaches an answer, a count or a deliverable.
        retired = retired_claim_refs(core)
        key = (row["n"], row["latest"], len(retired))
        if self._claims_cache is not None and self._claims_cache[0] == key:
            return self._claims_cache[1]
        claims = []
        for record in core.execute(
            "SELECT claim_version_id, claim_json, created_at FROM claim_versions ORDER BY created_at"
        ).fetchall():
            if record["claim_version_id"] in retired:
                continue
            claim = json.loads(record["claim_json"])
            subject = claim.get("subject_ref") or claim.get("company_ref") or ""
            claims.append({
                "ref": claim.get("id"), "claim_ref": claim.get("claim_ref"), "subject_ref": subject,
                "statement": claim.get("normalized_statement") or "", "period": claim.get("period"),
                "aspect": claim.get("metric_or_aspect"), "basis": claim.get("basis"), "kind": claim.get("claim_kind"),
                "value": claim.get("value"), "unit": claim.get("unit"), "status": claim.get("status"),
                "created_at": record["created_at"], "actor_ref": claim.get("actor_ref"),
            })
        self._claims_cache = (key, claims)
        return claims

    # -- labels ----------------------------------------------------------------

    @staticmethod
    def _members(mission: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        return {m["company_ref"]: dict(m) for m in mission["universe"]}

    @staticmethod
    def _label(members: Mapping[str, Mapping[str, Any]], company_ref: str | None) -> str:
        if not company_ref:
            return "行业"
        member = members.get(company_ref)
        if member is None:
            return "行业" if company_ref.startswith("industry:") else company_ref.rsplit(":", 1)[-1]
        ticker = member["ticker"]
        name = COMPANY_NAMES.get(ticker)
        return f"{ticker} · {name}" if name and name.upper() != ticker.upper() else ticker

    def _url_map(self) -> dict[str, dict[str, Any]]:
        """document_ref → {url, host, title} from fetch tickets and search summaries."""
        tickets = self.tickets.tickets()
        key = len(tickets)
        if self._url_cache is not None and self._url_cache[0] == key:
            return self._url_cache[1]
        urls: dict[str, dict[str, Any]] = {}
        for item in tickets:
            summary = item["summary"] or {}
            if item["lane"] == "discoveries":
                for found in summary.get("discovered_urls") or []:
                    if isinstance(found, dict) and found.get("document_ref"):
                        urls.setdefault(found["document_ref"], {})
                        urls[found["document_ref"]].update({k: found.get(k) for k in ("canonical_url", "title") if found.get(k)})
            elif item["lane"] == "fetches":
                ref = item["ticket"].get("document_ref")
                if ref and summary.get("canonical_url"):
                    urls.setdefault(ref, {})["canonical_url"] = summary["canonical_url"]
                    if summary.get("title"):
                        urls[ref]["title"] = summary["title"]
        for value in urls.values():
            value["host"] = _host(value.get("canonical_url", ""))
        self._url_cache = (key, urls)
        return urls

    def _document_label(self, document_ref: str | None, urls: Mapping[str, Mapping[str, Any]]) -> str:
        if not document_ref:
            return "一份文档"
        info = urls.get(document_ref)
        if info and info.get("title"):
            return f"{info['title']}（{info.get('host', '')}）"
        if info and info.get("canonical_url"):
            return info["canonical_url"]
        if document_ref.startswith("alphaengine-doc:"):
            return "一份卖方研报或电话会纪要"
        if document_ref.startswith("public-web"):
            return "一个公开网页"
        return "一份文档"

    def _figures(self, core: Any) -> dict[str, dict[str, Any]]:
        """Verified figures per company, counted by grade with a few examples.

        Read straight from the figure journal rather than from claims: these
        are held and citable now, and waiting for the Ledger admission path
        before showing them to the owner would hide work already done.
        """

        if not _table_exists(core, "coverage_mission_document_figures"):
            return {}
        out: dict[str, dict[str, Any]] = {}
        for row in core.execute(
            "SELECT company_ref,metric_ref,as_reported_label,period,value,unit,currency,"
            "scale,source_grade,document_ref,created_at FROM coverage_mission_document_figures "
            "ORDER BY created_at, figure_id"
        ).fetchall():
            entry = out.setdefault(row["company_ref"], {
                "total": 0, "by_grade": {}, "latest": [],
            })
            entry["total"] += 1
            grade = row["source_grade"]
            entry["by_grade"][grade] = entry["by_grade"].get(grade, 0) + 1
            entry["latest"].append({
                "metric_ref": row["metric_ref"], "label": row["as_reported_label"],
                "period": row["period"], "value": row["value"], "unit": row["unit"],
                "currency": row["currency"], "scale": row["scale"],
                "grade": grade, "grade_label": FIGURE_GRADE_LABELS.get(grade, grade),
                "document_ref": row["document_ref"], "at": row["created_at"],
            })
        for entry in out.values():
            entry["latest"] = entry["latest"][-6:][::-1]
        return out

    # -- overview ------------------------------------------------------------------

    def overview(self) -> dict[str, Any]:
        heartbeat = _load_json(self.config.heartbeat_path) or {}
        with self._core() as core:
            mission = self._mission(core)
            members = self._members(mission)
            claims = self._claims(core)
            versions = self._mission_versions(core, mission["mission_ref"])
            docs = {}
            for row in core.execute(
                "SELECT company_ref, status, COUNT(*) AS n FROM coverage_mission_discovered_documents "
                "WHERE mission_version_ref=? GROUP BY company_ref, status", (mission["id"],),
            ).fetchall():
                docs.setdefault(row["company_ref"], {})[row["status"]] = row["n"]
            reviews = {}
            for row in core.execute(
                "SELECT company_ref, state, COUNT(*) AS n FROM coverage_mission_document_reviews "
                "WHERE mission_version_ref=? GROUP BY company_ref, state", (mission["id"],),
            ).fetchall():
                reviews.setdefault(row["company_ref"], {})[row["state"]] = row["n"]
            theses = [json.loads(r["content_json"]) for r in core.execute(
                "SELECT content_json FROM thesis_versions ORDER BY created_at").fetchall()]
            figures = self._figures(core)
            stages = self._stage_rows(core, mission)
            documents = self._deliverables(core, mission)
        today = self.clock().date().isoformat()
        by_company: dict[str, list[dict[str, Any]]] = {}
        for claim in claims:
            by_company.setdefault(claim["subject_ref"], []).append(claim)
        companies = []
        for entry in stages:
            company_ref = entry["company_ref"]
            member = members.get(company_ref, {})
            d, r = docs.get(company_ref, {}), reviews.get(company_ref, {})
            found = sum(d.values())
            held = d.get("acquired", 0) + d.get("already_in_authority", 0)
            read = r.get("extraction_staged", 0) + r.get("dismissed", 0)
            waiting = r.get("awaiting_human_extraction", 0)
            own = by_company.get(company_ref, [])
            today_claims = sum(1 for c in own if c["created_at"][:10] == today)
            countable = [i for i in entry["items"] if i["status"] not in {"not_planned", "source_unavailable"}]
            done = [i for i in countable if i["status"] == "complete"]
            missing = [i for i in entry["items"] if i["status"] in {"partial", "missing"}]
            blocked = [i for i in entry["items"] if i["status"] in {"not_planned", "source_unavailable"}]
            if entry["stage"] is None:
                note = "还没有开始"
            elif missing:
                note = "还差：" + "、".join(f"{i['label']}（{i['have']}/{i['required']}）" for i in missing[:3])
            elif blocked:
                note = "能拿到的资料齐了；" + blocked[0]["note"]
            else:
                note = "资料底座齐了，等着写初步筛选"
            companies.append({
                "company_ref": company_ref, "ticker": member.get("ticker"), "name": COMPANY_NAMES.get(member.get("ticker", ""), ""),
                "priority": member.get("bootstrap_priority"), "tier": member.get("coverage_tier"),
                "stage": entry["stage_label"], "stage_ref": entry["stage"],
                "stage_status": entry["stage_status_label"], "note": note,
                "checklist": entry["items"],
                "document": documents.get(company_ref),
                "progress": {"found": found, "held": held, "read": read, "waiting": waiting,
                             "percent": int(round(100 * len(done) / len(countable))) if countable else 0},
                "claims": {"total": len(own), "today": today_claims,
                           "latest": [{"statement": c["statement"], "at": c["created_at"], "ref": c["ref"]}
                                      for c in own[-3:][::-1]]},
                # P11x: figures read out of this company's own documents, each
                # verified against the bytes it cited. Shown by grade, because
                # a number the company filed and a number someone said on a
                # call are both worth having and are not worth the same.
                "figures": figures.get(company_ref, _EMPTY_FIGURES),
            })
        planner = (heartbeat.get("bounded_planner") or {}).get("last_result") or {}
        discovery = planner.get("mission_source_discovery") or {}
        extraction = planner.get("document_extraction") or {}
        web = discovery.get("web_search") or {}
        budgets = {
            "model_calls": self._model_calls_today(mission, today),
            "alphaengine": ((discovery.get("acquisition") or {}).get("budget") or (discovery.get("discovery") or {}).get("budget")),
            "web": ((web.get("acquisition") or {}).get("budget") or (web.get("discovery") or {}).get("budget")),
            "mission": mission["budget"],
        }
        running = [self._ticket_event(t, members, self._url_map()) for t in self.tickets.tickets()
                   if _ticket_still_running(t["ticket"])]
        return {
            "schema_version": SCHEMA_VERSION, "as_of": _iso(self.clock()),
            "goal": {
                "mission_ref": mission["mission_ref"], "version": mission["version"], "id": mission["id"],
                "hash": mission["content_hash"], "title": mission["title"], "objective": mission["objective"],
                "research_questions": list(mission["research_questions"]),
                "deliverables": [STAGE_LABELS.get(d, d) for d in mission["deliverables"]],
                "industry_ref": mission["industry_ref"], "published_at": mission["created_at"],
                "sources": [{"source_ref": s["source_ref"], "label": SOURCE_LABELS.get(s["source_ref"], s["source_ref"]),
                             "role": s["role"], "connected": s["status"] == "connected"} for s in mission["source_plan"]],
                "history": versions,
            },
            "companies": companies,
            "totals": {
                "found": sum(c["progress"]["found"] for c in companies),
                "held": sum(c["progress"]["held"] for c in companies),
                "read": sum(c["progress"]["read"] for c in companies),
                "waiting": sum(c["progress"]["waiting"] for c in companies),
                "claims": len(claims), "claims_today": sum(1 for c in claims if c["created_at"][:10] == today),
                "theses": len(theses),
            },
            "theses": [{"ref": t.get("thesis_ref") or t.get("id"), "confidence": t.get("confidence"),
                        "summary": t.get("summary") or t.get("statement") or t.get("change_reason")} for t in theses],
            "activity": {
                "service_state": heartbeat.get("state"), "last_tick_at": heartbeat.get("last_tick_at"),
                "lanes": self._lane_states(heartbeat, extraction, discovery), "running": running,
            },
            "budgets": budgets,
            "model_available": self._model_status(),
        }

    def _stage_rows(self, core: sqlite3.Connection, mission: Mapping[str, Any]) -> list[dict[str, Any]]:
        """P10a:每家公司的阶段与资料底座清单，全部从任务自己的表里数出来。"""

        state: dict[str, dict[str, list[str]]] = {}
        for row in self._rows(core,
            "SELECT company_ref, stage_ref, status FROM coverage_mission_stage_records "
            "WHERE mission_version_ref=? ORDER BY created_at", (mission["id"],),
        ):
            state.setdefault(row["company_ref"], {}).setdefault(row["stage_ref"], []).append(row["status"])
        specs = planned_spec_refs_from_directory(self.config.state_dir / "discovery-plans")
        return evaluate_mission(core, mission, planned_specs=specs, stage_state=state)

    def _deliverables(self, core: sqlite3.Connection, mission: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        """P10c: each company's latest Initial Screen, as a card-sized summary."""

        result: dict[str, dict[str, Any]] = {}
        for row in self._rows(core,
            "SELECT v.record_json AS record_json FROM mission_deliverable_pointer p "
            "JOIN mission_deliverable_versions v ON v.version_id=p.version_id "
            "WHERE v.mission_version_ref=? AND v.kind='initial_screen'", (mission["id"],),
        ):
            record = json.loads(row["record_json"])
            written = [section for section in record["sections"] if section["body"]]
            result[record["subject_ref"]] = {
                "ref": record["deliverable_ref"], "version_ref": record["id"],
                "version": record["version"], "created_at": record["created_at"],
                "summary": record["summary"][:400],
                "sections_written": len(written), "sections_total": len(record["sections"]),
                "gaps": len(record["gaps"]),
                "claims_cited": len({ref for section in record["sections"] for ref in section["claim_refs"]}),
            }
        return result

    def document(self, version_ref: str) -> dict[str, Any]:
        """One deliverable, in full, for reading."""

        ref = _text(version_ref, "version_ref", maximum=512)
        with self._core() as core:
            rows = self._rows(core,
                "SELECT record_json, content_hash FROM mission_deliverable_versions WHERE version_id=?", (ref,))
            if not rows:
                raise CockpitError("这份文档不存在")
            record = json.loads(rows[0]["record_json"])
            if record["content_hash"] != rows[0]["content_hash"]:
                raise CockpitConflict("文档记录与哈希不符")
            mission = self._mission(core)
            members = self._members(mission)
            stage = [
                {"status": row["status"], "rationale": row["rationale"], "at": row["created_at"]}
                for row in self._rows(core,
                    "SELECT status, rationale, created_at FROM coverage_mission_stage_records "
                    "WHERE mission_version_ref=? AND company_ref=? AND stage_ref='initial_screen' "
                    "ORDER BY created_at", (record["mission_version_ref"], record["subject_ref"]))
            ]
            claims = {claim["ref"]: claim for claim in self._claims(core)}
        for section in record["sections"]:
            section["cited"] = [
                {"statement": claims[ref]["statement"], "period": claims[ref]["period"]}
                for ref in section["claim_refs"] if ref in claims
            ]
        return {
            **record, "company": self._label(members, record["subject_ref"]),
            "stage_history": stage, "as_of": _iso(self.clock()),
        }

    def _model_calls_today(self, mission: Mapping[str, Any], today: str) -> dict[str, Any] | None:
        if self.config.model_config_path is None:
            return None
        config = _load_json(self.config.model_config_path)
        if not isinstance(config, dict) or not config.get("budget_db"):
            return None
        try:
            with closing(sqlite3.connect(f"file:{config['budget_db']}?mode=ro", uri=True, timeout=5)) as ledger:
                ledger.row_factory = sqlite3.Row
                rows = ledger.execute(
                    "SELECT a.reserved_micros, s.actual_micros AS settled FROM thesis_impact_day_admissions a "
                    "JOIN model_mission_budget_bindings b ON b.admission_id=a.admission_id "
                    "LEFT JOIN thesis_impact_day_settlements s ON s.admission_id=a.admission_id "
                    "WHERE a.day=? AND b.mission_ref=?", (today, mission["mission_ref"]),
                ).fetchall()
        except sqlite3.Error:
            return None
        calls, micros = 0, 0
        for row in rows:
            calls += 1
            settled = row["settled"]
            micros += int(settled if isinstance(settled, int) else row["reserved_micros"])
        return {"used": calls, "cap": mission["budget"]["max_daily_paid_calls"],
                "cost_usd": round(micros / 1_000_000, 4), "cost_cap_usd": mission["budget"]["max_daily_cost_usd"]}

    @staticmethod
    def _lane_states(heartbeat: Mapping[str, Any], extraction: Mapping[str, Any], discovery: Mapping[str, Any]) -> list[dict[str, Any]]:
        def one(key: str, label: str, status: str | None, note: str) -> dict[str, Any]:
            return {"key": key, "label": label, "status": status or "idle", "note": note}
        web = discovery.get("web_search") or {}
        web_status = (web.get("discovery") or {}).get("status") or (web.get("acquisition") or {}).get("status")
        ae_status = (discovery.get("discovery") or {}).get("status") or (discovery.get("acquisition") or {}).get("status")
        awaiting = extraction.get("awaiting")
        last = extraction.get("last") or {}
        return [
            one("web", "搜索公开网页", web_status, "按公司轮流搜索并获取网页"),
            one("alphaengine", "获取研报与电话会", ae_status, "每 24 小时最多 30 次"),
            one("extraction", "阅读并提炼结论", extraction.get("status"),
                f"排队 {awaiting} 份" + (f"，上一轮读了 {len(last.get('drafted') or []) if isinstance(last.get('drafted'), list) else last.get('drafted', 0)} 段" if last else "")),
            one("weekly", "每周简报", (heartbeat.get("weekly_brief") or {}).get("state"), "每周四早上发到 Discord"),
        ]

    def _model_status(self) -> dict[str, Any]:
        if self.config.model_config_path is None or not self.config.model_config_path.exists():
            return {"available": False, "reason": "问答与目标拆解所需的模型尚未接入"}
        return {"available": True, "reason": None}

    # -- log -------------------------------------------------------------------------

    def _ticket_event(self, item: Mapping[str, Any], members: Mapping[str, Mapping[str, Any]],
                      urls: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
        lane, ticket, summary = item["lane"], item["ticket"], item["summary"] or {}
        status = ticket.get("status")
        state = "running" if status == "running" else "failed" if status in {"failed", "crashed"} or (
            ticket.get("exit_code") not in (None, 0)) else "done"
        auth = summary.get("authorization") or {}
        company_ref = ticket.get("company_ref") or summary.get("company_ref") or auth.get("company_ref")
        who = self._label(members, company_ref)
        detail, title = None, LANE_LABELS[lane]
        if lane == "discoveries":
            found = summary.get("discovered_urls") or summary.get("discovered_documents") or summary.get("document_refs") or []
            n = len(found) if isinstance(found, list) else 0
            title = f"为 {who} 搜索资料" if state == "running" else f"为 {who} 搜索资料，找到 {n} 条"
            queries = summary.get("queries") or summary.get("query") or ticket.get("query")
            if isinstance(queries, list):
                detail = "；".join(str(q.get("query") if isinstance(q, dict) else q) for q in queries[:3])
            elif isinstance(queries, str):
                detail = queries
        elif lane == "fetches":
            label = self._document_label(ticket.get("document_ref"), urls)
            title = ("正在获取网页" if state == "running" else "获取了网页" if state == "done" else "网页获取失败") + f"（{who}）"
            detail = label if summary.get("canonical_url") is None else summary["canonical_url"]
            if state == "failed" and summary.get("error"):
                detail = f"{detail} — {summary['error']}"
        elif lane == "acquisitions":
            chars = summary.get("content_chars")
            title = ("正在获取研报原文" if state == "running" else "获取了研报原文" if state == "done" else "研报获取失败") + f"（{who}）"
            detail = f"约 {chars:,} 字" if isinstance(chars, int) else None
        elif lane == "extractions":
            drafted = summary.get("drafted") or []
            admitted = summary.get("admitted") or []
            complete = summary.get("reviews_complete")
            if state == "running":
                title = "正在阅读原文并提炼结论"
            else:
                n_ok = sum(1 for d in drafted if isinstance(d, dict) and d.get("status") == "succeeded")
                title = f"读了 {len(drafted)} 段原文，写入 {sum(1 for a in admitted if a.get('status') == 'admitted')} 条结论"
                if not drafted and summary.get("stop_reason"):
                    title = {"nothing_to_draft": "没有新的原文可读", "drained": "本轮原文已读完"}.get(summary["stop_reason"], "阅读轮次结束")
                detail = f"完成 {complete} 份文档" if complete else None
                if not n_ok and drafted:
                    state = "failed"
        elif lane == "sec-lane-runs":
            issuers = ticket.get("issuers") or []
            title = f"读取了 SEC 财务数据（{'、'.join(issuers)}）" if state != "running" else "正在读取 SEC 财务数据"
        return {
            "id": f"ticket:{lane}:{item['dir']}",
            "at": ticket.get("completed_at") or ticket.get("started_at") or summary.get("created_at") or item.get("ticket_mtime"),
            "started_at": ticket.get("started_at"), "kind": lane, "lane": LANE_LABELS[lane], "title": title,
            "detail": detail, "state": state, "company": who if company_ref else None,
        }

    def log(self, *, since: str | None = None, limit: int = 150) -> dict[str, Any]:
        limit = max(1, min(int(limit), 500))
        with self._core() as core:
            mission = self._mission(core)
            members = self._members(mission)
            claims = self._claims(core)
            reviews = core.execute(
                "SELECT review_id, company_ref, document_ref, state, rationale, updated_at, candidate_claim_version_ref "
                "FROM coverage_mission_document_reviews WHERE state != 'awaiting_human_extraction' "
                "ORDER BY updated_at DESC LIMIT ?", (limit,),
            ).fetchall()
        urls = self._url_map()
        events: list[dict[str, Any]] = []
        for item in self.tickets.tickets():
            events.append(self._ticket_event(item, members, urls))
        for claim in claims[-limit:]:
            events.append({
                "id": f"claim:{claim['ref']}", "at": claim["created_at"], "kind": "claim", "lane": "新结论",
                "title": claim["statement"], "detail": None if not claim["basis"] else f"依据：{claim['basis']}",
                "state": "done", "company": self._label(members, claim["subject_ref"]),
            })
        for row in reviews:
            closed = row["state"] == "extraction_staged"
            admitted = re.search(r"(\d+) qualitative claim", row["rationale"] or "")
            refused = re.search(r"(\d+) (?:suggestion\(s\) )?refused", row["rationale"] or "")
            if closed:
                detail = f"入库 {admitted.group(1)} 条结论" if admitted else "结论已入库"
                if refused and refused.group(1) != "0":
                    detail += f"，另有 {refused.group(1)} 条未通过核验"
            else:
                detail = "这份文档里没有可用的观点" if "no admissible" in (row["rationale"] or "") else (row["rationale"] or "")[:200]
            events.append({
                "id": f"review:{row['review_id']}", "at": row["updated_at"], "kind": "review", "lane": "读完文档",
                "title": ("读完并入库：" if closed else "读完，没有可用结论：") + self._document_label(row["document_ref"], urls),
                "detail": detail, "state": "done" if closed else "skipped",
                "company": self._label(members, row["company_ref"]),
            })
        for row in self._rows_from(self.config.core_db,
            "SELECT record_json FROM mission_deliverable_versions ORDER BY created_at DESC LIMIT ?", (limit,),
        ):
            record = json.loads(row["record_json"])
            written = sum(1 for section in record["sections"] if section["body"])
            events.append({
                "id": f"deliverable:{record['id']}", "at": record["created_at"],
                "kind": "deliverable", "lane": "写文档",
                "title": f"写好了初步筛选第 {record['version']} 版（{written}/{len(record['sections'])} 节）",
                "detail": record["summary"][:200], "state": "done",
                "company": self._label(members, record["subject_ref"]),
            })
        for row in self._rows_from(self.config.core_db,
            "SELECT record_json FROM claim_retirement_decisions ORDER BY created_at DESC LIMIT ?", (limit,),
        ):
            record = json.loads(row["record_json"])
            retired = record["decision"] == "retired"
            events.append({
                "id": f"claim-decision:{record['id']}", "at": record["created_at"],
                "kind": "claim_decision", "lane": "账本更正",
                "title": ("退役了一条结论：" if retired else "保留了一条被标记的结论：") + (
                    CLAIM_REASON_LABELS.get(record["reason_code"], record["reason_code"])),
                "detail": record["rationale"], "state": "done" if retired else "skipped",
                "company": None,
            })
        for row in self.journal.rows("SELECT * FROM cockpit_events ORDER BY event_id DESC LIMIT ?", (limit,)):
            events.append({"id": f"cockpit:{row['event_id']}", "at": row["at"], "kind": row["kind"], "lane": "你",
                           "title": row["title"], "detail": row["detail"], "state": "done", "company": None})
        heartbeat = _load_json(self.config.heartbeat_path) or {}
        for key, label in (("bounded_planner", "研究调度"), ("outbox", "消息投递"), ("weekly_brief", "每周简报"), ("backup", "备份")):
            lane = heartbeat.get(key) or {}
            if lane.get("last_error"):
                events.append({"id": f"error:{key}:{lane.get('last_completed_at')}", "at": lane.get("last_completed_at") or heartbeat.get("last_tick_at"),
                               "kind": "problem", "lane": label, "title": f"{label}遇到问题", "detail": str(lane["last_error"])[:400],
                               "state": "failed", "company": None})
        events = [e for e in events if e.get("at")]
        if since:
            events = [e for e in events if e["at"] > since]
        events.sort(key=lambda e: (e["at"], e["id"]), reverse=True)
        events = events[:limit]
        return {"schema_version": SCHEMA_VERSION, "as_of": _iso(self.clock()), "events": events,
                "cursor": events[0]["at"] if events else since, "service_state": heartbeat.get("state"),
                "last_tick_at": heartbeat.get("last_tick_at")}

    # -- approvals -------------------------------------------------------------------

    def _rows_from(self, path: Path, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._core() as core:
            return self._rows(core, sql, params)

    @staticmethod
    def _rows(core: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        """Rows, or none when this Core predates the table (test fixtures, older states)."""
        try:
            return core.execute(sql, params).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return []
            raise

    def approvals(self) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        with self._core() as core:
            mission = self._mission(core)
            members = self._members(mission)
            for row in self._rows(core,
                "SELECT c.* FROM thesis_admission_candidates c LEFT JOIN thesis_admission_decisions d "
                "ON d.candidate_id=c.candidate_id WHERE d.decision_id IS NULL ORDER BY c.created_at",
            ):
                content = json.loads(row["content_json"])
                items.append({
                    "kind": "thesis", "ref": row["candidate_id"], "hash": row["record_hash"], "at": row["created_at"],
                    "title": f"是否接受研究论点：{row['thesis_ref'].split(':', 1)[-1]}",
                    "who": self._label(members, row["company_ref"]),
                    "summary": content.get("summary") or content.get("statement") or content.get("change_reason") or "",
                    "details": {"信心": content.get("confidence"), "证伪条件": content.get("falsifier_refs"),
                                "催化剂": content.get("catalyst_refs"), "提议者": row["proposed_by"]},
                    "actions": [{"decision": "admit", "label": "接受"}, {"decision": "reject", "label": "拒绝"}],
                    "needs_rationale": True,
                })
            for row in self._rows(core,
                "SELECT p.* FROM capability_proposal_versions p LEFT JOIN capability_decisions d ON d.revision_id=p.revision_id "
                "WHERE d.decision_id IS NULL ORDER BY p.created_at",
            ):
                proposal = json.loads(row["proposal_json"])
                evaluation = core.execute("SELECT evaluation_id FROM capability_evaluations WHERE revision_id=? ORDER BY created_at DESC LIMIT 1",
                                          (row["revision_id"],)).fetchone()
                items.append({
                    "kind": "capability", "ref": row["revision_id"], "hash": row["content_hash"], "at": row["created_at"],
                    "title": f"是否启用新工具：{row['capability_ref'].split(':', 1)[-1]}", "who": "系统",
                    "summary": proposal.get("summary") or proposal.get("description") or proposal.get("rationale") or "",
                    "details": {"申请的权限": proposal.get("requested_permissions") or proposal.get("permissions"),
                                "已有评估": bool(evaluation)},
                    "evaluation_id": evaluation["evaluation_id"] if evaluation else None,
                    "actions": [{"decision": "approve", "label": "批准", "disabled": evaluation is None,
                                 "hint": None if evaluation else "还没有评估结果，暂时不能批准"},
                                {"decision": "reject", "label": "拒绝"}],
                    "needs_rationale": True,
                })
            for row in self._rows(core,
                "SELECT p.* FROM bounded_planner_proposal_versions p "
                "LEFT JOIN bounded_planner_proposal_decisions d ON d.proposal_ref=p.proposal_id "
                "LEFT JOIN bounded_planner_terminal_events t ON t.loop_version_ref=p.loop_version_ref "
                "WHERE d.decision_id IS NULL AND t.event_id IS NULL AND p.loop_version_ref IN ("
                " SELECT v.version_id FROM bounded_planner_loop_versions v WHERE v.version_number="
                " (SELECT MAX(w.version_number) FROM bounded_planner_loop_versions w WHERE w.loop_ref=v.loop_ref)) "
                "ORDER BY p.created_at",
            ):
                record = json.loads(row["record_json"])
                items.append({
                    "kind": "planner", "ref": row["proposal_id"], "hash": row["content_hash"], "at": row["created_at"],
                    "title": "是否允许下一步探查" if row["action_kind"] == "probe" else "是否结束这条研究线",
                    "who": "研究调度", "summary": record.get("rationale") or record.get("summary") or "",
                    "details": {"轮次": row["round_ordinal"], "动作": record.get("action") or record.get("probe")},
                    "actions": [{"decision": "accept", "label": "允许"}], "needs_rationale": False,
                })
            for row in self._rows(core,
                "SELECT r.* FROM forecast_reconciliations r LEFT JOIN forecast_overturn_decisions d "
                "ON d.reconciliation_ref=r.reconciliation_id WHERE d.decision_id IS NULL AND r.band='overturn_candidate' "
                "ORDER BY r.created_at",
            ):
                record = json.loads(row["record_json"])
                items.append({
                    "kind": "forecast", "ref": row["reconciliation_id"], "hash": row["content_hash"], "at": row["created_at"],
                    "title": f"实际结果偏离预测：{row['metric_ref'].split(':', 1)[-1]}",
                    "who": self._label(members, row["subject_ref"]),
                    "summary": record.get("summary") or f"{row['period_start']} 至 {row['period_end']} 的实际值超出了预测线的容忍带。",
                    "details": {"偏离": record.get("deviation"), "预测线": row["forecast_line_ref"]},
                    "actions": [{"decision": "keep_forecast", "label": "维持预测"}, {"decision": "revise_forecast", "label": "修订预测"}],
                    "needs_rationale": True,
                })
        with self._core() as core:
            # P10b: a Claim the detectors flagged, waiting for you to retire or keep it.
            for row in self._rows(core,
                "SELECT c.record_json AS record_json, c.content_hash AS hash, "
                "v.claim_json AS claim_json FROM claim_retirement_challenges c "
                "LEFT JOIN claim_retirement_decisions d ON d.challenge_ref=c.challenge_id "
                "JOIN claim_versions v ON v.claim_version_id=c.claim_version_ref "
                "WHERE d.decision_id IS NULL ORDER BY c.created_at",
            ):
                record = json.loads(row["record_json"])
                claim = json.loads(row["claim_json"])
                items.append({
                    "kind": "claim", "ref": record["id"], "hash": row["hash"],
                    "at": record["created_at"],
                    "title": "这条结论可能不该留在账本里",
                    "who": self._label(members, record["subject_ref"]),
                    "summary": claim["normalized_statement"],
                    "details": {"发现的问题": CLAIM_REASON_LABELS.get(record["reason_code"], record["reason_code"]),
                                "依据": record["rationale"]},
                    "actions": [{"decision": "retired", "label": "退役这条结论"},
                                {"decision": "kept", "label": "保留"}],
                    "needs_rationale": False,
                })
        for row in self.journal.rows("SELECT * FROM cockpit_drafts WHERE status='open' ORDER BY created_at"):
            draft = json.loads(row["draft_json"])
            items.append({
                "kind": f"draft:{row['kind']}", "ref": row["draft_id"], "hash": row["content_hash"], "at": row["created_at"],
                "title": "待你确认的新研究目标" if row["kind"] == "goal" else "待你确认的方向调整",
                "who": "你", "summary": draft.get("summary") or "", "details": {},
                "actions": [{"decision": "publish", "label": "确认发布"}, {"decision": "discard", "label": "放弃"}],
                "needs_rationale": False,
            })
        items.sort(key=lambda i: i["at"])
        return {"schema_version": SCHEMA_VERSION, "as_of": _iso(self.clock()), "items": items, "count": len(items)}

    def decide(self, login: str, value: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise CockpitError("decision must be an object")
        kind = _text(value.get("kind"), "kind", maximum=64)
        ref = _text(value.get("ref"), "ref", maximum=512)
        digest = _sha(value.get("hash"), "hash")
        decision = _text(value.get("decision"), "decision", maximum=32)
        rationale = value.get("rationale") or ""
        if not isinstance(rationale, str) or len(rationale) > 4000:
            raise CockpitError("rationale must be text under 4000 characters")
        request_id = _text(value.get("request_id"), "request_id", maximum=128)
        actor = _subject_for_login(login)
        if kind.startswith("draft:"):
            return self._decide_draft(login, kind.split(":", 1)[1], ref, digest, decision, request_id)
        if kind == "thesis":
            if decision not in {"admit", "reject"}:
                raise CockpitError("decision must be admit or reject")
            if not rationale.strip():
                raise CockpitError("请写一句理由")
            operation, params = "decide_thesis_admission", {
                "candidate_id": ref, "candidate_hash": digest, "verdict": decision, "rationale": rationale.strip(),
                "decision_id": f"thesis-admission-decision:cockpit:{content_hash({'candidate': ref, 'request': request_id})[:24]}"}
            title = ("接受了研究论点" if decision == "admit" else "拒绝了研究论点") + f"：{ref.split(':', 1)[-1]}"
        elif kind == "capability":
            if decision not in {"approve", "reject"}:
                raise CockpitError("decision must be approve or reject")
            if not rationale.strip():
                raise CockpitError("请写一句理由")
            evaluation = value.get("evaluation_id")
            operation, params = "decide_capability_promotion", {
                "proposal_ref": ref, "decision": decision, "rationale": rationale.strip(),
                "decision_id": f"capability-decision:cockpit:{content_hash({'proposal': ref, 'request': request_id})[:24]}",
                **({"evaluation_id": evaluation} if isinstance(evaluation, str) and evaluation else {})}
            title = ("批准了新工具" if decision == "approve" else "拒绝了新工具") + f"：{ref}"
        elif kind == "planner":
            if decision != "accept":
                raise CockpitError("planner proposals can only be accepted here")
            operation, params = "bounded_planner_admit_proposal", {"proposal_ref": ref}
            title = "允许了研究调度的下一步"
        elif kind == "claim":
            if decision not in {"retired", "kept"}:
                raise CockpitError("decision must be retired or kept")
            operation, params = "decide_claim_retirement", {
                "challenge_ref": ref, "challenge_hash": digest, "decision": decision,
                "rationale": rationale.strip() or ("你确认退役这条结论" if decision == "retired" else "你确认保留这条结论")}
            title = ("退役了一条结论" if decision == "retired" else "保留了一条被标记的结论")
        elif kind == "forecast":
            if decision not in {"keep_forecast", "revise_forecast"}:
                raise CockpitError("decision must be keep_forecast or revise_forecast")
            if not rationale.strip():
                raise CockpitError("请写一句理由")
            operation, params = "decide_forecast_overturn", {
                "reconciliation_ref": ref, "reconciliation_hash": digest, "decision": decision, "rationale": rationale.strip(),
                "idempotency_key": f"cockpit-overturn:{ref}:{request_id}"}
            title = ("维持了预测" if decision == "keep_forecast" else "决定修订预测") + f"：{ref}"
        else:
            raise CockpitError("unknown approval kind")
        try:
            result = self.governance_call(self.token_config, self.writer_socket, actor_ref=actor, operation=operation, params=params)
        except (GovernanceCliError, RemoteError) as exc:
            raise CockpitConflict(f"这项决定没有被接受：{_reason(exc)}") from exc
        self.journal.record_event(kind="approval", title=title, detail=rationale.strip() or None, login=login,
                                  refs={"kind": kind, "ref": ref, "decision": decision, "operation": operation})
        return {"status": "decided", "kind": kind, "ref": ref, "decision": decision,
                "result": result if isinstance(result, (dict, list)) else None}

    # -- jobs (model work off the request thread) -----------------------------------------

    def _model_instance(self) -> CockpitModel:
        if self._model is not None:
            return self._model
        if self.config.model_config_path is None:
            raise CockpitError("问答与目标拆解所需的模型尚未接入")
        config = _load_json(self.config.model_config_path)
        if not isinstance(config, dict):
            raise CockpitError("模型配置无法读取")
        factory = self._model_factory or (lambda c: CockpitModel(c, scheduler_db=self.config.scheduler_db))
        self._model = factory(config)
        return self._model

    def _start_job(self, kind: str, login: str, request: Mapping[str, Any], runner: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        job_id = f"cockpit-job:{secrets.token_hex(12)}"
        now = _iso(self.clock())
        job = {"job_id": job_id, "kind": kind, "login": login, "status": "running", "request": dict(request),
               "result": None, "error": None, "created_at": now, "updated_at": now, "_expires": time.monotonic() + JOB_TTL_SECONDS}
        with self._jobs_lock:
            expired = [k for k, v in self._jobs.items() if v["_expires"] <= time.monotonic()]
            for key in expired:
                self._jobs.pop(key, None)
            if len(self._jobs) >= MAX_JOBS:
                raise CockpitError("too many requests are in flight; try again in a while")
            self._jobs[job_id] = job
        self.journal.write("INSERT INTO cockpit_jobs(job_id,kind,login,status,request_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                           (job_id, kind, login, "running", json.dumps(dict(request), ensure_ascii=False), now, now))

        def run() -> None:
            try:
                result = runner()
                status, error = "done", None
            except (CockpitError, CockpitModelError) as exc:
                result, status, error = None, "failed", str(exc)
            except Exception as exc:  # noqa: BLE001 - surfaced to the owner, never swallowed
                result, status, error = None, "failed", f"{type(exc).__name__}: {exc}"
            at = _iso(self.clock())
            with self._jobs_lock:
                job.update({"status": status, "result": result, "error": error, "updated_at": at})
            self.journal.write("UPDATE cockpit_jobs SET status=?, result_json=?, error=?, updated_at=? WHERE job_id=?",
                               (status, None if result is None else json.dumps(result, ensure_ascii=False), error, at, job_id))

        threading.Thread(target=run, name=f"dalton-cockpit-{kind}", daemon=True).start()
        return self._public_job(job)

    @staticmethod
    def _public_job(job: Mapping[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in job.items() if not k.startswith("_")}

    def job(self, login: str, job_id: str) -> dict[str, Any]:
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            if job is not None:
                if job["login"] != login:
                    raise CockpitError("job not found")
                return self._public_job(job)
        rows = self.journal.rows("SELECT * FROM cockpit_jobs WHERE job_id=? AND login=?", (job_id, login))
        if not rows:
            raise CockpitError("job not found")
        row = rows[0]
        return {"job_id": row["job_id"], "kind": row["kind"], "login": row["login"], "status": row["status"],
                "request": json.loads(row["request_json"]), "result": None if row["result_json"] is None else json.loads(row["result_json"]),
                "error": row["error"], "created_at": row["created_at"], "updated_at": row["updated_at"]}

    def history(self, login: str, kind: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.journal.rows("SELECT * FROM cockpit_jobs WHERE kind=? AND login=? ORDER BY created_at DESC LIMIT ?",
                                 (kind, login, max(1, min(int(limit), 100))))
        return [{"job_id": r["job_id"], "status": r["status"], "request": json.loads(r["request_json"]),
                 "result": None if r["result_json"] is None else json.loads(r["result_json"]), "error": r["error"],
                 "created_at": r["created_at"]} for r in rows]

    # -- ask -----------------------------------------------------------------------------------

    def ask(self, login: str, value: Mapping[str, Any]) -> dict[str, Any]:
        question = _text(value.get("question"), "question", maximum=2000)
        request_id = _text(value.get("request_id"), "request_id", maximum=128)
        model = self._model_instance()
        return self._start_job("ask", login, {"question": question, "request_id": request_id},
                               lambda: self._answer(model, login, question, request_id))

    def _answer(self, model: CockpitModel, login: str, question: str, request_id: str) -> dict[str, Any]:
        with self._core() as core:
            mission = self._mission(core)
            claims = self._claims(core)
            theses = [json.loads(r["content_json"]) for r in core.execute("SELECT content_json FROM thesis_versions ORDER BY created_at").fetchall()]
        members = self._members(mission)
        selected = self._select_claims(question, claims, members)
        prompt = self._ask_prompt(question, mission, members, selected, theses)
        call = model.call(purpose="ask", request_id=request_id, prompt=prompt, mission=mission)
        parsed = unwrap_json_object(call["text"]) or {}
        answer = parsed.get("answer") if isinstance(parsed.get("answer"), str) else call["text"].strip()
        cited: list[dict[str, Any]] = []
        raw_refs = parsed.get("citations") if isinstance(parsed.get("citations"), list) else []
        index = {f"C{i + 1}": c for i, c in enumerate(selected)}
        for item in raw_refs:
            claim = index.get(str(item).strip())
            if claim is not None and claim not in cited:
                cited.append({"tag": str(item).strip(), "statement": claim["statement"], "company": self._label(members, claim["subject_ref"]),
                              "period": claim["period"], "at": claim["created_at"], "ref": claim["ref"]})
        gaps = [str(g) for g in parsed.get("gaps", [])] if isinstance(parsed.get("gaps"), list) else []
        confidence = parsed.get("confidence") if parsed.get("confidence") in {"high", "medium", "low"} else None
        result = {"question": question, "answer": answer, "citations": cited, "gaps": gaps, "confidence": confidence,
                  "claims_considered": len(selected), "claims_total": len(claims), "cost_usd": round(call["cost_micros"] / 1_000_000, 4),
                  "replayed": call["replayed"], "answered_at": _iso(self.clock())}
        self.journal.record_event(kind="question", title=f"你问了：{question[:120]}", detail=answer[:300], login=login,
                                  refs={"job_kind": "ask", "request_id": request_id, "work_order_ref": call["work_order_ref"]})
        return result

    @staticmethod
    def _select_claims(question: str, claims: list[dict[str, Any]], members: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
        lowered = question.lower()
        words = set(re.findall(r"[a-z]+", lowered))
        mentioned = {ref for ref, m in members.items()
                     if m["ticker"].lower() in words
                     or (m["ticker"] in COMPANY_NAMES and COMPANY_NAMES[m["ticker"]].lower() in lowered)}
        pool = [c for c in claims if c["subject_ref"] in mentioned] if mentioned else list(claims)
        if mentioned and len(pool) < 20:
            pool = pool + [c for c in claims if c["subject_ref"] not in mentioned]
        pool = pool[-MAX_CLAIMS_IN_PROMPT:]
        budget, chosen = MAX_PROMPT_CHARS, []
        for claim in reversed(pool):
            cost = len(claim["statement"]) + 80
            if budget - cost < 0:
                break
            budget -= cost
            chosen.append(claim)
        chosen.reverse()
        return chosen

    def _ask_prompt(self, question: str, mission: Mapping[str, Any], members: Mapping[str, Mapping[str, Any]],
                    claims: list[dict[str, Any]], theses: list[Mapping[str, Any]]) -> str:
        lines = [
            "You are the research assistant of an equity research system. Answer the owner's question using ONLY the",
            "formal Claims and Theses listed below. Every Claim carries a tag like C12; cite the tags you rely on.",
            "If the Claims do not answer the question, say so plainly and list what is missing. Never invent facts,",
            "numbers or sources. Answer in the same language as the question (Chinese if the question is Chinese).",
            "Return raw JSON only, no markdown fence, with this exact shape:",
            '{"answer": "<answer text, may use short paragraphs and - bullets>", "citations": ["C1", "C7"],',
            ' "confidence": "high|medium|low", "gaps": ["<what the Ledger lacks to answer better>"]}',
            "",
            f"Research goal: {mission['title']} — {mission['objective']}",
            "Standing research questions: " + " | ".join(mission["research_questions"]),
            "Companies: " + ", ".join(f"{m['ticker']} ({COMPANY_NAMES.get(m['ticker'], '')})" for m in members.values()),
            "",
        ]
        if theses:
            lines.append("Theses:")
            for thesis in theses:
                lines.append(f"- [{thesis.get('confidence')}] {thesis.get('thesis_ref') or thesis.get('id')}: "
                             f"{thesis.get('summary') or thesis.get('statement') or thesis.get('change_reason')}")
            lines.append("")
        lines.append(f"Claims ({len(claims)}), oldest first:")
        for i, claim in enumerate(claims):
            who = members.get(claim["subject_ref"], {}).get("ticker") or claim["subject_ref"].split(":", 1)[-1]
            value = "" if claim["value"] is None else f" value={claim['value']} {claim['unit'] or ''}".rstrip()
            lines.append(f"C{i + 1} [{who}; {claim['period']}; {claim['created_at'][:10]}]{value} {claim['statement']}")
        lines += ["", f"Question: {question}"]
        return "\n".join(lines)

    # -- goal and steering drafts ---------------------------------------------------------------

    def draft(self, login: str, kind: str, value: Mapping[str, Any]) -> dict[str, Any]:
        if kind not in {"goal", "steer"}:
            raise CockpitError("unknown draft kind")
        text = _text(value.get("text"), "text", maximum=4000)
        request_id = _text(value.get("request_id"), "request_id", maximum=128)
        model = self._model_instance()
        return self._start_job(kind, login, {"text": text, "request_id": request_id},
                               lambda: self._draft(model, login, kind, text, request_id))

    def _draft(self, model: CockpitModel, login: str, kind: str, text: str, request_id: str) -> dict[str, Any]:
        with self._core() as core:
            mission = self._mission(core)
        members = self._members(mission)
        prompt = self._goal_prompt(text, mission, members) if kind == "goal" else self._steer_prompt(text, mission, members)
        call = model.call(purpose=kind, request_id=request_id, prompt=prompt, mission=mission)
        parsed = unwrap_json_object(call["text"])
        if parsed is None:
            raise CockpitError("模型没有给出可用的拆解结果，请换个说法再试")
        draft = self._normalize_draft(kind, parsed, mission)
        draft["based_on"] = {"mission_version_ref": mission["id"], "mission_version_hash": mission["content_hash"]}
        draft_id = f"cockpit-draft:{kind}:{secrets.token_hex(8)}"
        digest = content_hash(draft)
        now = _iso(self.clock())
        self.journal.write(
            "INSERT INTO cockpit_drafts(draft_id,kind,login,input_text,draft_json,content_hash,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (draft_id, kind, login, text, json.dumps(draft, ensure_ascii=False, sort_keys=True), digest, "open", now, now))
        return {"draft_id": draft_id, "draft_hash": digest, "kind": kind, "draft": draft, "input_text": text,
                "cost_usd": round(call["cost_micros"] / 1_000_000, 4), "replayed": call["replayed"], "created_at": now}

    @staticmethod
    def _questions(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        out = []
        for item in value:
            if isinstance(item, str) and item.strip():
                out.append(item.strip()[:400])
            elif isinstance(item, dict) and isinstance(item.get("question"), str) and item["question"].strip():
                out.append(item["question"].strip()[:400])
        return out[:12]

    def _normalize_draft(self, kind: str, parsed: Mapping[str, Any], mission: Mapping[str, Any]) -> dict[str, Any]:
        summary = parsed.get("summary") if isinstance(parsed.get("summary"), str) else ""
        if kind == "goal":
            title = parsed.get("title") if isinstance(parsed.get("title"), str) and parsed["title"].strip() else mission["title"]
            objective = parsed.get("objective") if isinstance(parsed.get("objective"), str) and parsed["objective"].strip() else mission["objective"]
            questions = self._questions(parsed.get("research_questions")) or list(mission["research_questions"])
            companies = []
            for item in parsed.get("suggested_companies") or []:
                if isinstance(item, dict) and isinstance(item.get("ticker"), str):
                    companies.append({"ticker": item["ticker"].strip().upper()[:12], "reason": str(item.get("reason") or "")[:300],
                                      "already_covered": item["ticker"].strip().upper() in {m["ticker"] for m in mission["universe"]}})
            subtasks = [str(s)[:300] for s in parsed.get("subtasks", []) if isinstance(s, (str, dict))][:12] \
                if isinstance(parsed.get("subtasks"), list) else []
            return {"kind": "goal", "summary": summary[:1200], "title": title.strip()[:200], "objective": objective.strip()[:2000],
                    "research_questions": questions, "suggested_companies": companies[:12], "subtasks": subtasks,
                    "changes": {"title": title.strip() != mission["title"], "objective": objective.strip() != mission["objective"],
                                "research_questions": questions != list(mission["research_questions"])}}
        add = self._questions(parsed.get("add_questions"))
        remove = self._questions(parsed.get("remove_questions"))
        current = list(mission["research_questions"])
        kept = [q for q in current if q not in remove]
        for q in add:
            if q not in kept:
                kept.append(q)
        objective = parsed.get("objective") if isinstance(parsed.get("objective"), str) and parsed["objective"].strip() else mission["objective"]
        return {"kind": "steer", "summary": summary[:1200], "understood_as": str(parsed.get("understood_as") or "")[:600],
                "add_questions": add, "remove_questions": [q for q in remove if q in current],
                "research_questions": kept[:12], "objective": objective.strip()[:2000],
                "not_possible": [str(x)[:300] for x in parsed.get("not_possible", [])][:6] if isinstance(parsed.get("not_possible"), list) else [],
                "changes": {"objective": objective.strip() != mission["objective"], "research_questions": kept[:12] != current}}

    @staticmethod
    def _mission_block(mission: Mapping[str, Any], members: Mapping[str, Mapping[str, Any]]) -> list[str]:
        return [
            f"Current goal title: {mission['title']}",
            f"Current objective: {mission['objective']}",
            "Current research questions:",
            *[f"- {q}" for q in mission["research_questions"]],
            "Companies under coverage: " + ", ".join(f"{m['ticker']} ({COMPANY_NAMES.get(m['ticker'], '')})" for m in members.values()),
            "Connected sources: " + ", ".join(SOURCE_LABELS.get(s["source_ref"], s["source_ref"]) for s in mission["source_plan"] if s["status"] == "connected"),
        ]

    def _goal_prompt(self, text: str, mission: Mapping[str, Any], members: Mapping[str, Mapping[str, Any]]) -> str:
        return "\n".join([
            "You are the planning assistant of an autonomous equity research system. The owner states a new overall",
            "research goal in their own words. Turn it into a precise mission: a short title, a one-paragraph objective,",
            "3 to 8 concrete research questions the system can pursue by reading filings, sell-side reports, earnings",
            "calls and public web pages, and a list of the sub-tasks the system will run. Keep the language of the owner",
            "(Chinese if they wrote Chinese). If the owner names companies that are not under coverage, list them under",
            "suggested_companies with a reason; the coverage list itself is changed separately by the owner.",
            "Return raw JSON only, no markdown fence:",
            '{"summary": "<2-3 sentences telling the owner what you understood and what the system will do>",',
            ' "title": "...", "objective": "...", "research_questions": ["..."], "subtasks": ["..."],',
            ' "suggested_companies": [{"ticker": "XYZ", "reason": "..."}]}',
            "", *self._mission_block(mission, members), "", f"Owner's new goal: {text}",
        ])

    def _steer_prompt(self, text: str, mission: Mapping[str, Any], members: Mapping[str, Mapping[str, Any]]) -> str:
        return "\n".join([
            "You are the planning assistant of an autonomous equity research system. The owner gives a steering",
            "instruction: a direction to emphasise, de-emphasise, add or drop. The only levers you may pull are the",
            "mission's research questions (add or remove whole questions) and, if the direction changes the goal itself,",
            "a reworded objective. Keep the owner's language. Anything the owner asks that these levers cannot do",
            "(new data sources, new companies, budgets, tools) goes under not_possible so the owner knows.",
            "Return raw JSON only, no markdown fence:",
            '{"summary": "<2-3 sentences: what will change and why>", "understood_as": "<one sentence restating the instruction>",',
            ' "add_questions": ["..."], "remove_questions": ["<verbatim existing question>"], "objective": "<unchanged or reworded>",',
            ' "not_possible": ["..."]}',
            "", *self._mission_block(mission, members), "", f"Owner's steering instruction: {text}",
        ])

    def drafts(self, login: str, kind: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.journal.rows(
            "SELECT * FROM cockpit_drafts WHERE login=? AND (? IS NULL OR kind=?) ORDER BY created_at DESC LIMIT ?",
            (login, kind, kind, max(1, min(int(limit), 100))))
        return [{"draft_id": r["draft_id"], "draft_hash": r["content_hash"], "kind": r["kind"], "status": r["status"],
                 "input_text": r["input_text"], "draft": json.loads(r["draft_json"]), "published_ref": r["published_ref"],
                 "created_at": r["created_at"]} for r in rows]

    def _decide_draft(self, login: str, kind: str, draft_id: str, digest: str, decision: str, request_id: str) -> dict[str, Any]:
        if decision not in {"publish", "discard"}:
            raise CockpitError("decision must be publish or discard")
        if decision == "discard":
            self.journal.write("UPDATE cockpit_drafts SET status='discarded', updated_at=? WHERE draft_id=? AND content_hash=? AND status='open'",
                               (_iso(self.clock()), draft_id, digest))
            return {"status": "discarded", "draft_id": draft_id}
        return self.publish_draft(login, {"draft_id": draft_id, "draft_hash": digest, "request_id": request_id})

    def publish_draft(self, login: str, value: Mapping[str, Any]) -> dict[str, Any]:
        draft_id = _text(value.get("draft_id"), "draft_id", maximum=128)
        digest = _sha(value.get("draft_hash"), "draft_hash")
        request_id = _text(value.get("request_id"), "request_id", maximum=128)
        rows = self.journal.rows("SELECT * FROM cockpit_drafts WHERE draft_id=? AND login=?", (draft_id, login))
        if not rows:
            raise CockpitError("draft not found")
        row = rows[0]
        if row["content_hash"] != digest:
            raise CockpitConflict("the draft changed; reload and read it again")
        if row["status"] != "open":
            raise CockpitConflict("this draft was already " + ("published" if row["status"] == "published" else "discarded"))
        draft = json.loads(row["draft_json"])
        with self._core() as core:
            mission = self._mission(core)
        if draft["based_on"]["mission_version_ref"] != mission["id"] or draft["based_on"]["mission_version_hash"] != mission["content_hash"]:
            raise CockpitConflict("the research goal changed since this draft was written; draft it again")
        version = int(mission["version"]) + 1
        slug = mission["mission_ref"].split(":", 1)[1]
        params = {
            "mission_ref": mission["mission_ref"],
            **{field: json.loads(json.dumps(mission[field])) for field in (
                "title", "objective", "industry_ref", "universe", "research_questions", "deliverables",
                "source_plan", "bindings", "autonomy", "budget")},
            "version_id": f"coverage-mission-version:{slug}:{version}", "prior_version_ref": mission["id"],
            "idempotency_key": f"{mission['mission_ref']}:{version}:cockpit:{request_id}",
        }
        if row["kind"] == "goal":
            params.update({"title": draft["title"], "objective": draft["objective"], "research_questions": draft["research_questions"]})
            title = f"发布了新的研究目标：{draft['title']}"
        else:
            params.update({"objective": draft["objective"], "research_questions": draft["research_questions"]})
            title = "调整了研究方向：" + (draft.get("understood_as") or draft.get("summary") or "")[:160]
        try:
            result = self.governance_call(self.token_config, self.writer_socket, actor_ref=_subject_for_login(login),
                                          operation="create_coverage_mission", params=params)
        except (GovernanceCliError, RemoteError) as exc:
            raise CockpitConflict(f"发布没有被接受：{_reason(exc)}") from exc
        published = result.get("id") if isinstance(result, Mapping) else None
        self.journal.write("UPDATE cockpit_drafts SET status='published', published_ref=?, updated_at=? WHERE draft_id=?",
                           (published, _iso(self.clock()), draft_id))
        self.journal.record_event(kind="goal" if row["kind"] == "goal" else "steer", title=title,
                                  detail=draft.get("summary"), login=login,
                                  refs={"draft_id": draft_id, "mission_version_ref": published})
        return {"status": "published", "draft_id": draft_id, "mission_version_ref": published, "version": version}


def _reason(exc: BaseException) -> str:
    text = str(exc) or type(exc).__name__
    return text[:300]


def _subject_for_login(login: str) -> str:
    import hashlib
    return "human:tailscale-" + hashlib.sha256(login.encode("utf-8")).hexdigest()[:32]


__all__ = ["CockpitConfig", "CockpitConflict", "CockpitError", "CockpitJournal", "CockpitPlane", "LANE_LABELS"]
