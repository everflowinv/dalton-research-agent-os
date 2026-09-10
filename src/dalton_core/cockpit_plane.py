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
# What a directive asks for, in the owner's language.
PLAN_ACTION_LABELS = {
    "search": "去找", "acquire": "去取", "read": "去读",
    "extract_figures": "去抓数字", "stop": "停",
}
ITEM_LABELS = {
    "quarterly_financials": "季度财报数字", "earnings_calls": "电话会纪要",
    "annual_report": "年报正文", "broker_research": "券商观点",
    "industry_demand": "行业需求", "competitive_landscape": "竞争格局",
}
FIGURE_GRADE_LABELS = {
    "company-filed-document": "公司文件披露",
    "earnings-call-transcript": "电话会口述（未经财报核对）",
}
STAGE_LABELS = {
    "industry_framework": "行业框架", "initial_screen": "初步筛选", "industry_model": "行业模型",
    "company_model": "公司模型", "forecast_lines": "预测线", "investment_memo": "投资备忘录",
    "weekly_brief": "每周简报", "deep_insight_gate": "深度洞察", "continuous_coverage": "持续覆盖",
}
# -- P11a / P11c / P12b / P13-M2 / Q1 -----------------------------------------
#
# What the four Wave 1 lanes put in the Core, in the owner's language. Every
# reader below returns nothing when its lane's table is absent, so a Core from
# before that lane still shows the page it always showed rather than an error:
# "this Core has no price history" and "this company has no price history" are
# different answers, and only the second one is worth a note on a card.
VERDICT_LABELS = {
    "read": "读过", "useful": "有用", "needs_more_evidence": "证据不够",
    "disagree": "不同意", "revise": "要重写",
}
# The verdicts that say the last attempt was not enough.
OUTSTANDING_VERDICTS = frozenset({"needs_more_evidence", "disagree", "revise"})
IMPORTANCE_LABELS = {
    "filing": "公司报表原文", "management_statement": "管理层原话",
    "sell_side": "卖方观点", "news": "新闻报道", "other": "其他",
}
ASPECT_LABELS = {
    "business_model": "怎么赚钱", "segments_and_mix": "业务构成",
    "demand_drivers": "需求从哪来", "supply_and_cost": "成本与供给",
    "competitive_position": "竞争位置",
    "management_and_capital_allocation": "管理层与资本配置",
    "guidance_style": "指引风格", "kpi_dictionary": "关键指标口径",
    "catalyst_calendar": "日程与催化", "history_of_price_drivers": "股价的历史驱动",
    "industry": "行业", "other": "其他",
}
# P15d: the owner's language for a conviction call's three closed fields.
CALL_DIRECTION_LABELS = {"long": "做多", "short": "做空", "avoid": "回避"}
CALL_HORIZON_LABELS = {
    "3_6_months": "3–6 个月", "6_12_months": "6–12 个月",
    "1_3_years": "1–3 年", "3_5_years": "3–5 年",
}
CALL_STANDARD_LABELS = {
    "met": "达到手册的风险回报标准",
    "not_met": "没达到手册的风险回报标准",
    "unavailable": "没给出可对照的数字",
    "not_applicable": "手册对这类 call 没有回报标准",
}
# P11c asked for this one specifically: a percentile computed while the
# fundamentals never moved is the price's percentile wearing a multiple's
# clothes, and a reader who is not told cannot know.
PERCENTILE_BASIS_LABELS = {
    "price_only": "只反映股价高低（这段历史里基本面没有变过）",
    "price_and_filed_fundamentals": "股价与已报基本面一起算出来的",
}
CHANGE_REASON_LABELS = {
    "filing_actual": "财报数字取代了当初的估计",
    "driver_event": "有事件改变了驱动因素",
    "assumption_review": "复核了假设",
    "evidence_thicker": "证据变厚了",
    "human_revision": "人改的",
}
ASSUMPTION_KIND_LABELS = {"estimate": "模型估的", "human": "人写的", "actual": "已报实际"}
QUALITY_CHECK_LABELS = {
    "numbers_without_refs": "每个数字都有出处",
    "residual_citation_artefacts": "引用标记清理干净",
    "duplicate_parallel_citations": "同一件事没有被并列引用多次",
    "required_sections_present": "该写的章节都写了",
    "claim_refs_resolve": "引用的结论都找得到",
    "cites_only_shown_claims": "只引用了给它看过的结论",
    "confidence_stated": "说明了把握有多大",
    "every_section_cites": "每一节都有依据",
    "new_version_cites_new_refs": "新版本用上了新证据",
    "restatement_drift": "改写没有偏离原意",
}
# What each status means, in the owner's language. The driver's own ``reason``
# is English and written for whoever reads a tick summary -- "this mission does
# not grant market_price in autonomy.may_write" is the right sentence in the
# wrong place -- so it moves to a detail line and the owner reads this instead.
LANE_STATUS_NOTES = {
    "launched": "刚起了一个任务",
    "busy": "上一个任务还在跑",
    "idle": "装好了，这一轮没有要做的",
    "held": "上一次没成，暂时不再试同一件事",
    "rejected": "这次没被接受",
    "unconfigured": "这台机器上没装这条流水线",
    "ungranted": "研究目标还没授权它写入，所以一次也没跑",
    "unavailable": "这一轮读不到它需要的东西",
    "unstarted": "这台机器上还没有跑过这条流水线",
    "current": "已经是最新的了",
    "failed": "出错了",
}
# The lanes the registry knows about, named for the owner. A lane with no name
# here still appears -- silence about a lane is exactly what this panel exists
# to end -- under its own key, which is ugly but visible.
REGISTRY_LANE_LABELS = {
    "guidepoint_discovery": "找专家访谈纪要",
    "mission_sec_quarters": "取 SEC 季度数字",
    "mission_statements": "取三张报表",
    "mission_market_prices": "取每日股价",
    "mission_tracking": "每天盯着已覆盖的公司",
    "mission_catalyst_calendar": "记下公司下次开口的日子",
    "company_model_spec": "写公司模型的规格",
    "company_model_forecast": "算预测行",
    "claim_index": "给结论建索引",
    "research_plan": "决定下一步做什么",
    "initial_screen": "写初步筛选",
    "event_judgement": "判断新发生的事要不要动",
    "debate_map": "整理市场在吵什么、我们站哪边",
    "mission_crowd_sources": "看散户与员工在说什么",
    "mission_stage": "记录研究阶段",
    "claim_review": "复核已有结论",
    "sales_notes_feed": "读 sales note",
    "company_wiki_feed": "读公司维基与访谈纪要",
    "research_task": "做专项研究",
    "mission_reflection": "每周回头看时间花在哪",
    "company_dossier": "写公司档案",
    "conviction_call": "提出值得下注的判断，等你裁决",
    "mission_reopen": "看已过闸的公司够不够重写一版",
}
# Already shown by name above the registry rows, with their budgets.
LANES_SHOWN_ELSEWHERE = frozenset({"mission_source_discovery", "document_extraction"})
# -- INT2: P14a / C1 / P14e / P14-M / Q2, in the owner's language --------------
#
# The same rule as the Wave 1 block above: every reader below answers empty on
# a Core that never had the lane's table, so an older Core keeps the page it
# had. What is new here is that most of these words are *judgements* rather
# than counts, and a judgement shown in the machine's vocabulary is a
# judgement the owner cannot argue with.
EVENT_KIND_LABELS = {
    "price_move": "股价异动", "price_divergence": "股价与我们的判断持续背离",
    "news": "新闻", "filing": "公司报表", "transcript": "电话会纪要",
    "rating_change": "评级变化", "calendar": "日程",
    "reconciliation": "预测与实际对账", "claim": "新结论",
    "sales_note": "卖方 sales note", "crowd_post": "散户与市场议论",
    "expert_excerpt": "专家访谈摘录",
}
# Ordered best first, the same order the Playbook reads them in.
EVIDENCE_TIER_LABELS = {
    "primary_filing": "公司报表原文", "management_direct": "管理层原话",
    "expert_network": "专家访谈", "sell_side": "卖方观点",
    "vendor_note": "vendor 归一化", "internal_wiki": "我们自己的档案",
    "market_price": "市场价格", "derived": "我们算出来的",
    "news_media": "新闻报道", "crowd": "网上的议论",
}
# The five words the judgement layer may say, and the six things it may do.
JUDGEMENT_DECISION_LABELS = {
    "NO_CHANGE": "不用改主意", "THESIS_STRENGTHENED": "论点更站得住了",
    "THESIS_WEAKENED": "论点被削弱了", "THESIS_BROKEN": "论点被打破了",
    "NEW_THESIS": "这是一个新论点",
}
JUDGEMENT_ACTION_LABELS = {
    "no_change": "什么都不做", "note": "写一段短报告",
    "research": "派一次专项研究", "revise_forecast": "改预测",
    "revise_thesis": "提一个论点修订候选", "revise_dossier": "改公司档案",
}
# What the independent reader said about that decision. ``none`` is not a
# verdict: it is the absence of one, and the two must not look alike.
VERIFIER_VERDICT_LABELS = {
    "pass": "独立复核通过", "reject": "独立复核不通过",
    "none": "没有独立复核",
}
# What a source can hand over, in the words a person would use to ask for it.
CONTENT_KIND_LABELS = {
    "sell_side_report": "卖方研报", "sell_side_comment": "卖方短评",
    "transcript": "电话会纪要", "management_minutes": "管理层会议纪要",
    "expert_excerpt": "专家访谈摘录", "sales_note": "卖方 sales note",
    "crowd_post": "散户帖子", "employee_review": "员工评价",
    "news": "新闻", "filing": "公司报表", "financial_statement": "三张报表",
    "price": "股价", "consensus": "市场一致预期", "calendar": "日程",
    "web_page": "公开网页",
}
CONNECTION_STATUS_LABELS = {
    "connected": "已接上", "not_connected": "还没接上",
    "probe_only": "只允许试读", "undeclared": "研究目标里没提过它",
    "unknown": "状态不明",
}
COMPLETENESS_LABELS = {
    "enumerated": "能取全", "bounded": "能取到有限的一批", "sampled": "只能取到样本",
}
# The tracking policy's source keys, named for the owner. A key with no name
# here shows its key, which is ugly and visible -- the same rule the lane
# panel follows.
TRACKING_SOURCE_LABELS = {
    "yfinance": "股价", "sec": "SEC 报表与 8-K", "alphaengine": "卖方研报与纪要",
    "x-xreach": "X（推特）", "sales-notes": "卖方 sales note",
    "gemini-web-search": "公开网页搜索", "guidepoint": "专家访谈",
    "company-wiki": "我们自己的公司维基", "employee-reviews": "员工评价",
    "catalyst-calendar": "催化剂日历",
}
CATALYST_EVENT_LABELS = {
    "earnings": "业绩发布", "guidance": "指引", "investor_day": "投资者日",
    "filing_due": "报表到期", "ex_dividend": "除息日", "other": "其他",
}
# P14e: what a special-purpose research task ended up as.
RESEARCH_TASK_STATE_LABELS = {
    "admitted": "已排队，还没开跑", "running": "正在做", "terminal": "已结束",
}
RESEARCH_TASK_TERMINAL_LABELS = {
    "evidence_observed_for_review": "有发现，待复核",
    "coverage_complete_unobservable_candidate": "查遍了，没有可观察到的证据",
    "budget_exhausted": "预算用完，还没答完",
    "human_replan_required": "等人重新规划",
    "human_deprioritized": "人已降级",
}
# The two model tiers a purpose can sit in, and what each is for.
MODEL_TIER_LABELS = {
    "brain": "要动脑的（写判断、做规划）",
    "cheap": "量大而便宜的（逐窗口阅读、打标签）",
    "verifier": "独立复核的（必须与写的那个不是同一家）",
}
# ADR-0007 / P14d: the two human checkpoints that arrive with the revision
# loop. Rendered whenever their rows exist; the decision path is not assumed,
# because the branch that adds it is not this one.
CHECKPOINT_TABLES = {
    "thesis_revision_candidate": ("thesis_revision_candidates", "candidate_id",
                                  "thesis_revision_decisions", "candidate_ref"),
    "gate_reopen": ("gate_reopen_proposals", "proposal_id",
                    "gate_reopen_decisions", "proposal_ref"),
}
CHECKPOINT_TITLES = {
    "thesis_revision_candidate": "有事情发生，可能要改我们对这家公司的判断",
    "gate_reopen": "一道已经过掉的闸，现在有证据说可以重开",
}
# What each one's buttons say, when this Core can actually decide it. The
# words are the authorities' own verdict vocabularies -- ``accept / reject /
# defer`` (P14b) and ``approve / decline`` (P14d) -- so a button cannot offer
# something the writer would refuse.
CHECKPOINT_ACTIONS = {
    "thesis_revision_candidate": (
        {"decision": "accept", "label": "接受，出新版本"},
        {"decision": "reject", "label": "不接受"},
        {"decision": "defer", "label": "先放着，再看看"},
    ),
    "gate_reopen": (
        {"decision": "approve", "label": "重出一版"},
        {"decision": "decline", "label": "不重出"},
    ),
}
# And what it says instead, on a Core whose writer predates the decision ops.
CHECKPOINT_UNDECIDABLE_NOTES = {
    "thesis_revision_candidate": "这一项要人裁决，而这个 Core 上还没有裁决账本（ADR-0007）",
    "gate_reopen": "这一项要人裁决，而这个 Core 上还没有裁决账本（ADR-0008）",
}
# C2: the four pools a day's budget is split into, named for what each buys.
POOL_LABELS = {
    "coverage": "把公司读完（找、取、读、抽数字）",
    "event_response": "判断每天发生的事",
    "adhoc": "专项研究",
    "maintenance": "维护（打标签、复核、周报）",
}
# How many of each of these a company card carries. The card is a card.
MAX_EVENTS_ON_CARD = 8
MAX_JUDGEMENTS_ON_CARD = 5
MAX_REFLECTIONS_ON_CARD = 2
MAX_TASKS_ON_CARD = 5
# How many bars back the card's range change looks. About a trading year; the
# start date is always named beside it, because a percentage whose window the
# reader cannot see is a number they cannot check.
RANGE_BARS = 252
MAX_CLAIMS_IN_VIEW = 300

JOB_TTL_SECONDS = 6 * 3600
MAX_JOBS = 200
# P15a moved the answer's own bounds into ``ask_context`` (the byte budget and
# the claim-row cap live with the priority order that spends them).  These two
# stay as the names other readers import.
from .ask_context import DEFAULT_BUDGET_CHARS as MAX_PROMPT_CHARS, MAX_CLAIM_ROWS as MAX_CLAIMS_IN_PROMPT


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


# Which mission budget line caps each source's own calls. A source with no
# entry has no cap of its own; it spends the mission's shared paid-call budget.
SOURCE_DAILY_CAP_KEYS = {"source:alphaengine": "max_alphaengine_calls_24h"}


def _source_daily_cap(source_ref: str, budget) -> int | None:
    key = SOURCE_DAILY_CAP_KEYS.get(source_ref)
    if key is None or not isinstance(budget, Mapping):
        return None
    value = budget.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _effective_alphaengine_cap(discovery, budget) -> Mapping[str, Any] | None:
    """The cap actually in force, preferring what the lane last measured."""

    if isinstance(discovery, Mapping):
        for key in ("discovery", "acquisition"):
            lane = discovery.get(key)
            measured = lane.get("budget") if isinstance(lane, Mapping) else None
            if isinstance(measured, Mapping) and isinstance(measured.get("cap"), int):
                return measured
    requested = _source_daily_cap("source:alphaengine", budget)
    return None if requested is None else {"cap": requested, "bound_by": "mission"}


def _alphaengine_cap_note(cap: Mapping[str, Any] | None) -> str:
    if cap is None:
        return "上限未设置"
    note = f"每 24 小时最多 {cap['cap']} 次"
    if cap.get("bound_by") == "owner" and cap.get("mission_cap"):
        # Say it plainly: the owner raised a budget and something else is
        # holding it down. Silence here is how 30 looked like 130 for days.
        note += f"（任务预算 {cap['mission_cap']}，被程序内置的 owner 安全上限压到 {cap['cap']}）"
    return note


def _interval_label(seconds: Any) -> str | None:
    """A cadence in the words a person uses for it.

    "43200 秒" is the number the policy carries and not a frequency anybody
    reads; the owner's own table said "every trading day", "twice a day",
    "weekly", so those are the words.
    """

    if not isinstance(seconds, int) or isinstance(seconds, bool) or seconds <= 0:
        return None
    if seconds % 86400 == 0:
        days = seconds // 86400
        return "每天一次" if days == 1 else f"每 {days} 天一次"
    if seconds % 3600 == 0:
        hours = seconds // 3600
        if 24 % hours == 0:
            return f"每天 {24 // hours} 次"
        return f"每 {hours} 小时一次"
    return f"每 {max(1, seconds // 60)} 分钟一次"


def _table_exists(connection, name: str) -> bool:
    """Whether this Core has the table yet; a fresh deploy may not."""

    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,),
    ).fetchone() is not None


def _column_exists(connection, table: str, column: str) -> bool:
    """Whether this Core's copy of a table carries a column yet.

    The revision decision ledgers arrived in two shapes -- one that records a
    ``defer`` as a decision that leaves the candidate open, and an earlier one
    that had no such distinction -- so the page reads the shape rather than
    assuming which one it is looking at.
    """

    return any(
        row[1] == column
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    )


def _gate_reopen_view(record: Mapping[str, Any], summary: str) -> tuple[str, dict[str, Any]]:
    """P14d: the diff is the whole argument, so it is the summary.

    Tolerant of a record that carries only the little a hand-written or an
    older row has: the assessment's diff is what makes the case, and where
    there is none the row still says what it can rather than failing to
    render.
    """

    flipped = [
        f"{entry.get('label')}：{(entry.get('was') or {}).get('mark')}"
        f"（{(entry.get('was') or {}).get('value')}）"
        f" → {(entry.get('now') or {}).get('mark')}（{(entry.get('now') or {}).get('value')}）"
        for entry in record.get("diff") or ()
        if entry.get("flipped")
    ]
    passed_ref = record.get("passed_version_ref")
    details: dict[str, Any] = {
        "变化": flipped,
        "过闸的那一版": (None if passed_ref is None
                         else f"v{record.get('passed_version_number')}（{passed_ref}）"),
        "过闸时间": record.get("passed_at"),
        "改版理由": CHANGE_REASON_LABELS.get(
            record.get("change_reason"), record.get("change_reason")),
        "退步的项目": list(record.get("regressed") or ()),
    }
    return ("；".join(flipped) or summary or "证据底座有项目从缺变成了有。"), details


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
    # INT2 / P14-M: the broker's own catalog, so the page can say whether the
    # models this Core holds are the models the gateway offers. A path rather
    # than a convention: this process must not go looking for the host's
    # configuration on its own, and a Core installed without the gateway has
    # no catalog to compare against and says so.
    openclaw_config_path: Path | None = None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CockpitConfig":
        fields = {"core_db", "state_dir", "heartbeat_path", "scheduler_db", "journal_path",
                  "model_config_path", "mission_ref", "openclaw_config_path"}
        if not isinstance(raw, Mapping) or set(raw) - fields or not {"core_db", "state_dir", "heartbeat_path",
                                                                       "scheduler_db", "journal_path"} <= set(raw):
            raise CockpitError("cockpit config has an invalid shape")
        model = raw.get("model_config_path")
        broker = raw.get("openclaw_config_path")
        mission = raw.get("mission_ref")
        if mission is not None and (not isinstance(mission, str) or not mission.startswith("coverage-mission:")):
            raise CockpitError("mission_ref must name a coverage mission")
        return cls(
            core_db=_path(raw["core_db"], "core_db"), state_dir=_path(raw["state_dir"], "state_dir"),
            heartbeat_path=_path(raw["heartbeat_path"], "heartbeat_path"),
            scheduler_db=_path(raw["scheduler_db"], "scheduler_db"),
            journal_path=_path(raw["journal_path"], "journal_path"),
            model_config_path=None if model is None else _path(model, "model_config_path"),
            openclaw_config_path=(None if broker is None
                                  else _path(broker, "openclaw_config_path")),
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

    def _plan(self, core: Any, mission: Mapping[str, Any],
              members: Mapping[str, Any]) -> dict[str, Any] | None:
        """The system's own decision about what to work on next.

        P13w: the planner has been steering discovery for a while and the owner
        could not see it -- the plan lived in one table and its effects showed
        up only as lanes going quiet. A decision nobody can read is
        indistinguishable from the calendar it replaced, which is the whole
        objection this was built to answer.

        Shown with its reasons and its subjects in the owner's own names,
        because the point is that it can be argued with.
        """

        if not _table_exists(core, "coverage_mission_research_plans"):
            return None
        row = core.execute(
            "SELECT * FROM coverage_mission_research_plans WHERE mission_version_ref=? "
            "ORDER BY created_at DESC, plan_id DESC LIMIT 1", (mission["id"],),
        ).fetchone()
        if row is None:
            return None

        def subject(ref: Any) -> str:
            if ref == mission.get("industry_ref"):
                return "整个行业"
            return self._label(members, ref)

        directives = [
            {"rank": d.get("rank"), "subject": subject(d.get("company_ref")),
             "item": ITEM_LABELS.get(d.get("item_ref"), d.get("item_ref")),
             "action": d.get("action"),
             "action_label": PLAN_ACTION_LABELS.get(d.get("action"), d.get("action")),
             "reason": d.get("reason")}
            for d in json.loads(row["directives_json"])
        ]
        inquiries = [
            {"subject": subject(q.get("company_ref")) if q.get("company_ref") else "整个行业",
             "question": q.get("question"), "wants": q.get("wants"),
             "because": q.get("because")}
            for q in json.loads(row["inquiries_json"])
        ]
        return {
            "plan_ref": row["plan_id"], "decided_at": row["created_at"],
            "assessment": row["assessment"],
            "directives": directives, "inquiries": inquiries,
            "stopped": sum(1 for d in directives if d["action"] == "stop"),
        }

    def _figures(self, core: Any) -> dict[str, dict[str, Any]]:
        """Verified figures per company, counted by grade with a few examples.

        Read straight from the figure journal rather than from claims: these
        are held and citable now, and waiting for the Ledger admission path
        before showing them to the owner would hide work already done.
        """

        if not _table_exists(core, "coverage_mission_document_figures"):
            return {}
        out: dict[str, dict[str, Any]] = {}
        retracted = _table_exists(
            core, "coverage_mission_document_figure_retractions")
        query = (
            "SELECT f.company_ref,f.metric_ref,f.as_reported_label,f.period,f.value,"
            "f.unit,f.currency,f.scale,f.source_grade,f.document_ref,f.created_at "
            "FROM coverage_mission_document_figures f "
        )
        if retracted:
            # A withdrawn figure is not shown: the owner asked for the wrong
            # ones gone, and a wrong number on the page is worse than none.
            query += (
                "LEFT JOIN coverage_mission_document_figure_retractions r "
                "ON r.figure_id=f.figure_id WHERE r.figure_id IS NULL "
            )
        query += "ORDER BY f.created_at, f.figure_id"
        for row in core.execute(query).fetchall():
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

    # -- what the Wave 1 lanes wrote, per company ----------------------------
    #
    # Each of these reads one lane's own table out of the read-only Core and
    # answers ``{}`` when that table is not there. The cockpit is installed on
    # Cores older than every one of these lanes, and a page that raises rather
    # than degrades is a page the owner cannot use to find out why.

    @staticmethod
    def _latest_by(core: Any, table: str, key: str, order: str) -> list[sqlite3.Row]:
        """The newest row per ``key``, without parsing the ones it replaced.

        Written as a join rather than "read them all and keep the last"
        because a price series carries three years of bars in every version:
        parsing the superseded ones costs the whole history, twice, to throw
        it away.
        """

        return core.execute(
            f"SELECT t.* FROM {table} t JOIN (SELECT {key} AS k, MAX({order}) AS n "
            f"FROM {table} GROUP BY {key}) newest ON newest.k=t.{key} "
            f"AND newest.n=t.{order}"
        ).fetchall()

    def _market(self, core: Any) -> dict[str, dict[str, Any]]:
        """P11a: the latest close, when it is from, and whether it settled.

        The provisional flag is the point of showing this at all. A price
        pulled mid-session has exactly the shape of a close and is not one,
        and a card that prints it without saying so is the page telling the
        owner the market closed at a number it never closed at.
        """

        if not _table_exists(core, "market_price_series_versions"):
            return {}
        from .market_price import bar_is_provisional

        out: dict[str, dict[str, Any]] = {}
        for row in self._latest_by(
            core, "market_price_series_versions", "series_ref", "version_number"
        ):
            record = json.loads(row["record_json"])
            bars = record.get("bars") or []
            if not bars:
                continue
            newest, window = bars[-1], bars[-RANGE_BARS:]
            first = window[0]
            try:
                change = round(
                    (float(newest["close"]) / float(first["close"]) - 1) * 100, 1)
            except (TypeError, ValueError, ZeroDivisionError):
                change = None
            provisional = bar_is_provisional(newest)
            out[record["company_ref"]] = {
                "as_of": newest["date"], "close": newest["close"],
                "adj_close": newest["adj_close"], "currency": record.get("currency"),
                "provisional": provisional,
                "note": ("这是盘中价，当天还没有收盘定价"
                         if provisional else "收盘价"),
                "bars": len(bars), "since": record.get("first_bar_date"),
                "change_percent": change, "change_since": first["date"],
                "version": record.get("version"),
            }
        return out

    def _valuation(self, core: Any) -> dict[str, dict[str, Any]]:
        """P11c: the four multiples, each with its percentile and its basis.

        ``basis`` travels with the number rather than in a footnote: a
        percentile computed over a stretch in which the filed fundamentals
        never moved is the price's own percentile, and a reader who is not
        told that will read it as "cheap against its own history".
        """

        if not _table_exists(core, "valuation_snapshot_versions"):
            return {}
        out: dict[str, dict[str, Any]] = {}
        for row in self._latest_by(
            core, "valuation_snapshot_versions", "snapshot_ref", "version_number"
        ):
            record = json.loads(row["record_json"])
            metrics = []
            for item in record.get("metrics") or []:
                percentile = item.get("percentile") or {}
                basis = percentile.get("basis")
                metrics.append({
                    "metric": item.get("metric"), "label": item.get("label"),
                    "unit": item.get("unit"), "status": item.get("status"),
                    "value": item.get("value"),
                    # An unavailable metric shows the sentence saying why,
                    # never a blank: a blank reads as a zero.
                    "reason": item.get("reason"),
                    "percentile": percentile.get("value"),
                    "percentile_basis": basis,
                    "percentile_basis_label": PERCENTILE_BASIS_LABELS.get(basis),
                    "percentile_reason": percentile.get("reason"),
                    "sample_size": percentile.get("sample_size"),
                })
            basis = record.get("basis") or {}
            dated = basis.get("shares_basis") == "dated_shares"
            out[record["company_ref"]] = {
                "as_of": record.get("as_of"), "currency": record.get("currency"),
                "version": record.get("version"),
                "available": sum(1 for m in metrics if m["status"] == "available"),
                "metrics": metrics,
                "basis": {
                    "market_cap": basis.get("market_cap"),
                    "market_cap_formula": basis.get("market_cap_formula"),
                    "fundamental_windows": basis.get("fundamental_window_count"),
                    "price_history_bars": basis.get("price_history_bars"),
                    "shares_basis": basis.get("shares_basis"),
                    "shares_note": ("股本按各期实际观测" if dated else
                                    "历史市值用的是今天的股本（没有历史股本来源）"),
                    "percentile_method": basis.get("percentile_method"),
                },
            }
        return out

    def _forecast(self, core: Any) -> dict[str, dict[str, Any]]:
        """P13-M2: how much of each company's model actually stands up."""

        if not _table_exists(core, "forecast_model_versions"):
            return {}
        from .model_forecast_driver import model_readiness

        out: dict[str, dict[str, Any]] = {}
        for row in self._latest_by(
            core, "forecast_model_versions", "model_ref", "version_number"
        ):
            record = json.loads(row["record_json"])
            readiness = model_readiness(record)
            reason = record.get("change_reason")
            kinds = readiness.get("assumption_kinds") or {}
            out[record["company_ref"]] = {
                "version": record.get("version"), "version_ref": record.get("id"),
                "created_at": record.get("created_at"),
                "change_reason": reason,
                "change_reason_label": CHANGE_REASON_LABELS.get(reason, reason),
                "decision": record.get("decision"),
                "evidence_count": len(record.get("evidence_refs") or []),
                "readiness": readiness,
                "assumptions_by_kind": {
                    ASSUMPTION_KIND_LABELS.get(kind, kind): count
                    for kind, count in kinds.items()
                },
                "note": self._forecast_note(readiness),
            }
        return out

    @staticmethod
    def _forecast_note(readiness: Mapping[str, Any]) -> str:
        """One sentence, counted rather than scored.

        P13-M2 refused to put a percentage on a model and this refuses too:
        "80% modelled" invites the owner to accept a model with no cost line
        in it.
        """

        missing = list(readiness.get("results_unavailable") or [])
        head = (f"{readiness.get('forecast_quarters', 0)} 个未来季度，"
                f"{readiness.get('drivers_with_assumptions', 0)}/"
                f"{readiness.get('drivers', 0)} 条驱动因素有假设")
        if missing:
            return head + "；算不出来的行：" + "、".join(missing[:3])
        return head + "；这条链上的每一行都算出来了"

    def _quality(self, core: Any) -> dict[str, dict[str, Any]]:
        """Q1: the newest score of every artefact that has one, by target."""

        if not _table_exists(core, "research_quality_score_versions"):
            return {}
        from .research_quality_rubrics import PASSING_SCORE, RUBRICS

        out: dict[str, dict[str, Any]] = {}
        for row in self._latest_by(
            core, "research_quality_score_versions", "score_ref", "version_number"
        ):
            record = json.loads(row["record_json"])
            rubric = RUBRICS.get(record.get("rubric_ref"))
            questions = ({c.criterion_id: c.question for c in rubric.criteria}
                         if rubric is not None else {})
            deterministic = record.get("deterministic") or {}
            judge = record.get("judge") or {}
            verifier = record.get("verifier") or {}
            summary = judge.get("summary") or {}
            criteria = [{
                "criterion_id": item.get("criterion_id"),
                "question": questions.get(item.get("criterion_id"), item.get("criterion_id")),
                "score": item.get("score"), "evidence": item.get("evidence"),
                "below_passing": isinstance(item.get("score"), int)
                and item["score"] < PASSING_SCORE,
            } for item in judge.get("scores") or []]
            out[record["target_ref"]] = {
                "target_ref": record["target_ref"],
                "target_hash": record.get("target_hash"),
                "target_kind": record.get("artefact_kind"),
                "subject_ref": record.get("subject_ref"),
                "rubric": rubric.title if rubric is not None else record.get("rubric_ref"),
                "rubric_ref": record.get("rubric_ref"),
                "scored_at": record.get("created_at"),
                "checks": [{
                    "check": item.get("check"),
                    "label": QUALITY_CHECK_LABELS.get(item.get("check"), item.get("check")),
                    "status": item.get("status"), "count": item.get("count"),
                } for item in deterministic.get("checks") or []],
                "checks_passed": deterministic.get("passed"),
                # The stored word, not one inferred from the rows: a judge
                # that refused says so, and a score decided by the checks
                # alone was never judged at all.
                "judge_status": judge.get("status", "refused") if judge else "not_judged",
                "mean": summary.get("mean"), "minimum": summary.get("minimum"),
                "criteria": criteria,
                "below_passing": [c["question"] for c in criteria if c["below_passing"]],
                # An independent reader confirmed this reading, or nobody did.
                "verified": verifier.get("status") == "verified",
            }
        return out

    def _journal(self, core: Any) -> dict[str, Any]:
        """Q1: what the PM said, by artefact and by company."""

        if not _table_exists(core, "analyst_journal_entries"):
            return {"by_target": {}, "by_company": {}, "enabled": False}
        by_target: dict[str, list[dict[str, Any]]] = {}
        by_company: dict[str, dict[str, Any]] = {}
        for row in self._rows(core,
            "SELECT target_ref, target_kind, company_ref, verdict, note, created_at, "
            "entry_number FROM analyst_journal_entries ORDER BY created_at, entry_number",
        ):
            entry = {
                "target_ref": row["target_ref"], "target_kind": row["target_kind"],
                "verdict": row["verdict"],
                "verdict_label": VERDICT_LABELS.get(row["verdict"], row["verdict"]),
                "note": row["note"], "at": row["created_at"],
                "number": row["entry_number"],
                "outstanding": row["verdict"] in OUTSTANDING_VERDICTS,
            }
            by_target.setdefault(row["target_ref"], []).append(entry)
            if row["company_ref"]:
                bucket = by_company.setdefault(
                    row["company_ref"], {"total": 0, "outstanding": 0, "latest": []})
                bucket["total"] += 1
                bucket["outstanding"] += int(entry["outstanding"])
                bucket["latest"] = ([entry] + bucket["latest"])[:3]
        return {"by_target": by_target, "by_company": by_company, "enabled": True}

    # -- INT2: what the tracking, calendar, task and reflection lanes wrote ----
    #
    # Same discipline as the Wave 1 readers above: one lane's own table, read
    # out of the read-only Core, empty when the table is not there. None of
    # these constructs its lane's authority -- every one of those wants a
    # ``DaltonStore`` and runs its schema script on the way in, which a
    # read-only connection cannot do and a cockpit must never want to.

    def _tracking_policy(self) -> dict[str, Any] | None:
        """The installed cadence policy, or the packaged one, or nothing.

        The installed copy wins: it is what the lane actually runs on, and a
        page that shows the repo's baselines while the machine runs someone
        else's is a page that lies quietly.
        """

        from .tracking_cadence import POLICY_PATH, load_policy, TrackingCadenceError

        for candidate in (self.config.state_dir / "tracking-policy.json", POLICY_PATH):
            try:
                if not candidate.is_file():
                    continue
                return load_policy(candidate)
            except (OSError, TrackingCadenceError):
                continue
        return None

    @staticmethod
    def _event_summary(kind: str, payload: Mapping[str, Any]) -> str:
        """One line about what happened, in the words the payload carries."""

        if kind == "price_move":
            direction = "涨" if payload.get("direction") == "up" else "跌"
            return (f"{payload.get('as_of')} {direction} "
                    f"{payload.get('return_percent')}%（收 {payload.get('close')}）")
        if kind == "price_divergence":
            return (f"{payload.get('window_days')} 个交易日里相对同业累计 "
                    f"{payload.get('excess_vs_basket_percent')}%，与我们的判断相反")
        if kind == "rating_change":
            return (f"{payload.get('broker')}：{payload.get('from_rating')} → "
                    f"{payload.get('to_rating')}")
        if kind == "calendar":
            confirmed = "已确认" if payload.get("confirmed") else "日期未确认"
            return f"{payload.get('expected_date')} {payload.get('event_kind')}（{confirmed}）"
        if kind == "reconciliation":
            return (f"{payload.get('metric_ref')} {payload.get('period_end')} 偏离 "
                    f"{payload.get('deviation_percent')}%")
        if kind == "claim":
            return str(payload.get("statement") or payload.get("claim_ref") or "")[:200]
        return str(payload.get("title") or payload.get("document_ref") or "")[:200]

    def _events(self, core: Any) -> dict[str, dict[str, Any]]:
        """P14a: what happened to each company, by kind and with its tier."""

        if not _table_exists(core, "research_events"):
            return {}
        out: dict[str, dict[str, Any]] = {}
        for row in self._rows(core,
            "SELECT company_ref, kind, evidence_tier, occurred_at, record_json "
            "FROM research_events ORDER BY occurred_at, event_id",
        ):
            entry = out.setdefault(row["company_ref"], {
                "total": 0, "by_kind": {}, "latest": [],
            })
            entry["total"] += 1
            entry["by_kind"][row["kind"]] = entry["by_kind"].get(row["kind"], 0) + 1
            record = json.loads(row["record_json"])
            tier = row["evidence_tier"]
            entry["latest"].append({
                "kind": row["kind"],
                "kind_label": EVENT_KIND_LABELS.get(row["kind"], row["kind"]),
                "tier": tier, "tier_label": EVIDENCE_TIER_LABELS.get(tier, tier),
                "occurred_at": row["occurred_at"],
                "summary": self._event_summary(row["kind"], record.get("payload") or {}),
                "ref": record.get("id"),
            })
        for entry in out.values():
            entry["latest"] = entry["latest"][-MAX_EVENTS_ON_CARD:][::-1]
            # Ordered by the Playbook's own evidence order rather than by
            # count: "what kind of thing happened" reads top-down.
            entry["kinds"] = [
                {"kind": kind, "label": EVENT_KIND_LABELS.get(kind, kind),
                 "count": entry["by_kind"][kind]}
                for kind in EVENT_KIND_LABELS if kind in entry["by_kind"]
            ]
        return out

    def _judgements(self, core: Any) -> dict[str, Any]:
        """P14a: the brain's decision about each event, and what it did."""

        empty: dict[str, Any] = {"by_company": {}, "by_ref": {}, "enabled": False}
        if not _table_exists(core, "event_judgements"):
            return empty
        by_company: dict[str, dict[str, Any]] = {}
        by_ref: dict[str, dict[str, Any]] = {}
        for row in self._rows(core,
            "SELECT judgement_id, company_ref, record_json, created_at "
            "FROM event_judgements ORDER BY created_at, judgement_id",
        ):
            record = json.loads(row["record_json"])
            decision, action = record.get("decision"), record.get("action")
            verifier = record.get("verifier") or {}
            verdict = verifier.get("verdict") or verifier.get("status") or "none"
            effect = record.get("effect") or {}
            item = {
                "ref": row["judgement_id"], "at": row["created_at"],
                "event_ref": record.get("event_ref"),
                "event_kind": record.get("event_kind"),
                "event_kind_label": EVENT_KIND_LABELS.get(
                    record.get("event_kind"), record.get("event_kind")),
                "decision": decision,
                "decision_label": JUDGEMENT_DECISION_LABELS.get(decision, decision),
                "action": action,
                "action_label": JUDGEMENT_ACTION_LABELS.get(action, action),
                # The one-line reason, which is the whole point of showing a
                # decision at all: a verdict with no because is an assertion.
                "because": record.get("because"),
                "note": record.get("note"),
                "citations": list(record.get("citations") or ()),
                # What actually landed, not what was intended: a queued effect
                # and a published one are different facts.
                "effect": effect.get("status") or effect.get("kind"),
                "effect_detail": effect.get("reason") or effect.get("ref"),
                "verifier_verdict": verdict,
                "verifier_label": VERIFIER_VERDICT_LABELS.get(verdict, verdict),
            }
            by_ref[row["judgement_id"]] = item
            bucket = by_company.setdefault(
                row["company_ref"], {"total": 0, "latest": []})
            bucket["total"] += 1
            bucket["latest"].append(item)
        for bucket in by_company.values():
            bucket["latest"] = bucket["latest"][-MAX_JUDGEMENTS_ON_CARD:][::-1]
        return {"by_company": by_company, "by_ref": by_ref, "enabled": True}

    def _reflections(self, core: Any) -> dict[str, Any]:
        """P14a: what we expected, what happened, and what we may have missed."""

        empty: dict[str, Any] = {"by_company": {}, "by_judgement": {}, "enabled": False}
        if not _table_exists(core, "thesis_reflections"):
            return empty
        by_company: dict[str, list[dict[str, Any]]] = {}
        by_judgement: dict[str, dict[str, Any]] = {}
        for row in self._rows(core,
            "SELECT reflection_id, judgement_ref, company_ref, record_json, created_at "
            "FROM thesis_reflections ORDER BY created_at, reflection_id",
        ):
            record = json.loads(row["record_json"])
            market = record.get("market_view_vs_ours") or {}
            item = {
                "ref": row["reflection_id"], "at": row["created_at"],
                "judgement_ref": row["judgement_ref"],
                "trigger": record.get("trigger_kind"),
                "trigger_label": ("股价一直和我们的判断相反"
                                  if record.get("trigger_kind") == "price_divergence"
                                  else "我们改了主意"),
                "what_we_expected": record.get("what_we_expected"),
                "what_happened": record.get("what_happened"),
                "why": record.get("why"),
                "missed_debates": [
                    {"question": item.get("question"), "refs": list(item.get("refs") or ())}
                    for item in record.get("missed_debates") or ()
                ],
                "followup_tracking": [
                    {"source_key": item.get("source_key"),
                     "source_label": TRACKING_SOURCE_LABELS.get(
                         item.get("source_key"), item.get("source_key")),
                     "interval_label": _interval_label(item.get("interval_seconds")),
                     "because": item.get("because")}
                    for item in record.get("followup_tracking") or ()
                ],
                "followup_research": [
                    {"question": item.get("question"), "wants": item.get("wants")}
                    for item in record.get("followup_research") or ()
                ],
                # Absent consensus is said out loud rather than left blank:
                # with no consensus authority in this Core the honest answer
                # is "we have no street view to compare ourselves against".
                "market_view": {
                    "available": bool(market.get("available")),
                    "summary": (market.get("summary") if market.get("available")
                                else "这个 Core 里还没有街上的看法可比"),
                    "our_direction": market.get("our_direction"),
                },
                "convergence_pathway": record.get("convergence_pathway"),
                # A follow-up here changed nothing: it is a candidate, and the
                # card says so where the owner reads it.
                "note": "跟进项只是候选：它没有改任何频率，也没有开任何任务",
            }
            by_judgement[row["judgement_ref"]] = item
            by_company.setdefault(row["company_ref"], []).append(item)
        return {
            "by_company": {ref: rows[-MAX_REFLECTIONS_ON_CARD:][::-1]
                           for ref, rows in by_company.items()},
            "by_judgement": by_judgement, "enabled": True,
        }

    def _catalysts(self, core: Any) -> dict[str, dict[str, Any]]:
        """C1: the next thing each company will say, and how many days out."""

        if not _table_exists(core, "catalyst_calendar_versions"):
            return {}
        from .catalyst_calendar import CatalystCalendarAuthority

        today = self.clock().date().isoformat()
        out: dict[str, dict[str, Any]] = {}
        for row in self._latest_by(
            core, "catalyst_calendar_versions", "calendar_ref", "version_number"
        ):
            version = json.loads(row["record_json"])
            forthcoming = [entry for entry in version.get("entries") or ()
                           if entry.get("expected_date", "") >= today]
            if not forthcoming:
                continue
            entry = min(forthcoming, key=lambda item: (
                item["expected_date"], item["event_kind"], item["anchor_date"]))
            # The lane's own reader view rather than a second computation of
            # the caveat here: the caveat travels with the date by design.
            view = CatalystCalendarAuthority._reader_view(version, entry, today)
            kind = view.get("event_kind")
            out[version["company_ref"]] = {
                "event_kind": kind,
                "event_label": CATALYST_EVENT_LABELS.get(kind, kind),
                "expected_date": view.get("expected_date"),
                "days_until": view.get("days_until"),
                "headline": f"下一个催化剂 T−{view.get('days_until')} 天",
                "confidence": view.get("confidence"),
                "date_unconfirmed": view.get("date_unconfirmed"),
                # "日期未确认" when the vendor guessed it, empty when the
                # company announced it. T-22 next to a guess and T-22 next to
                # an announcement look identical without this.
                "date_caveat": view.get("date_caveat"),
                "disagreement": view.get("disagreement"),
                "disagreeing_dates": list(view.get("disagreeing_dates") or ()),
                "version_ref": view.get("version_ref"),
            }
        return out

    def _cadences(self, core: Any, policy: Mapping[str, Any] | None
                  ) -> dict[str, list[dict[str, Any]]]:
        """P14a: how often we look at each source for each company, and why.

        The baseline is shown for every source in the policy, and the brain's
        own version replaces it where one exists. Showing only the versions
        would hide every source nobody has re-timed, which is most of them.
        """

        if policy is None:
            return {}
        baseline = {key: dict(value) for key, value in policy["cadences"].items()}
        chosen: dict[str, dict[str, dict[str, Any]]] = {}
        if _table_exists(core, "tracking_cadence_versions"):
            for row in self._latest_by(
                core, "tracking_cadence_versions", "cadence_ref", "version_number"
            ):
                record = json.loads(row["record_json"])
                chosen.setdefault(record["company_ref"], {})[record["source_key"]] = record
        out: dict[str, list[dict[str, Any]]] = {}
        for company_ref, records in chosen.items():
            out[company_ref] = self._cadence_rows(baseline, records)
        return {"__baseline__": self._cadence_rows(baseline, {}), **out}

    @staticmethod
    def _cadence_rows(baseline: Mapping[str, Mapping[str, Any]],
                      records: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
        rows = []
        for key, entry in baseline.items():
            record = records.get(key)
            seconds = int((record or entry)["interval_seconds"])
            rows.append({
                "source_key": key,
                "source_label": TRACKING_SOURCE_LABELS.get(key, key),
                "interval_seconds": seconds,
                "interval_label": _interval_label(seconds),
                "baseline_label": _interval_label(int(entry["interval_seconds"])),
                # The policy's own sentence when nobody has re-timed it; the
                # brain's when it has. Both are the reason for this number.
                "because": (record or entry).get("because"),
                "adjustable": bool(entry.get("adjustable")),
                "adjustable_label": ("大脑可以调" if entry.get("adjustable")
                                     else "固定，大脑不能调"),
                "decided_by_brain": record is not None,
                "version": None if record is None else record.get("version"),
                "at": None if record is None else record.get("created_at"),
            })
        return rows

    def _research_tasks(self, core: Any) -> dict[str, list[dict[str, Any]]]:
        """P14e: 正在专项研究 X / 预算用了多少 / 结论或缺口, per company.

        Read out of the loop tables directly. ``research_task_view`` wants a
        ``BoundedPlannerAuthority``, which wants a write handle and runs its
        schema script; the cockpit has neither and should not acquire one to
        answer a question about rows that are already there.
        """

        if not _table_exists(core, "bounded_planner_loop_versions"):
            return {}
        rounds: dict[str, int] = {}
        for row in self._rows(core,
            "SELECT loop_version_ref, COUNT(*) AS n FROM bounded_research_plan_rounds "
            "GROUP BY loop_version_ref",
        ):
            rounds[row["loop_version_ref"]] = row["n"]
        terminal: dict[str, str] = {
            row["loop_version_ref"]: row["terminal_state"] for row in self._rows(core,
                "SELECT loop_version_ref, terminal_state FROM bounded_planner_terminal_events")
        }
        questions: dict[str, dict[str, Any]] = {
            row["version_id"]: {"question": row["question"],
                                "company_ref": row["company_ref"]}
            for row in self._rows(core,
                "SELECT version_id, question, company_ref FROM backlog_question_versions")
        }
        out: dict[str, list[dict[str, Any]]] = {}
        for row in self._latest_by(
            core, "bounded_planner_loop_versions", "loop_ref", "version_number"
        ):
            record = json.loads(row["record_json"])
            admission = record.get("admission") or {}
            if admission.get("source") != "inquiry":
                continue
            question = questions.get(record.get("question_version_ref")) or {}
            used = rounds.get(row["version_id"], 0)
            budget = record.get("budget") or {}
            state = ("terminal" if row["version_id"] in terminal
                     else ("running" if used else "admitted"))
            end = terminal.get(row["version_id"])
            out.setdefault(question.get("company_ref") or "unknown", []).append({
                "task_ref": record.get("loop_ref"), "at": record.get("created_at"),
                "question": question.get("question"),
                "state": state,
                "state_label": RESEARCH_TASK_STATE_LABELS.get(state, state),
                "rounds_used": used,
                "rounds_budget": budget.get("max_rounds"),
                "budget_label": f"{used}/{budget.get('max_rounds')} 轮",
                "conclusion": None if end is None
                else RESEARCH_TASK_TERMINAL_LABELS.get(end, end),
                # A task with no conclusion yet has a gap, and the gap is the
                # honest answer to "what did it find".
                "gap": None if end is not None
                else ("还在做" if used else "已排队，尚未开跑"),
            })
        return {ref: rows[:MAX_TASKS_ON_CARD] for ref, rows in out.items()}

    def _governance_records(self) -> dict[str, str | None]:
        """Every installed connector record and whether the owner approved it."""

        directory = self.config.state_dir / "connector-governance"
        out: dict[str, str | None] = {}
        try:
            names = sorted(directory.glob("*.json"))
        except OSError:
            return out
        for path in names:
            record = _load_json(path)
            out[path.name] = (record.get("status")
                              if isinstance(record, Mapping) else None)
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
            plan = self._plan(core, mission, members)
            stages = self._stage_rows(core, mission)
            documents = self._deliverables(core, mission)
            # Wave 1: the four lanes' own tables. Each answers {} on a Core
            # that never had that lane, so the card degrades to the card it
            # was rather than to an error page.
            market = self._market(core)
            valuation = self._valuation(core)
            forecasts = self._forecast(core)
            quality = self._quality(core)
            journal = self._journal(core)
            # INT2: P14a's daily tracking, C1's calendar and P14e's tasks.
            # Same degradation rule: {} on a Core without the lane's table.
            policy = self._tracking_policy()
            events = self._events(core)
            judgements = self._judgements(core)
            reflections = self._reflections(core)
            catalysts = self._catalysts(core)
            cadences = self._cadences(core, policy)
            tasks = self._research_tasks(core)
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
            deliverable = documents.get(company_ref)
            if deliverable is not None:
                deliverable = {
                    **deliverable,
                    # Q1: what the rubric said about this exact document, and
                    # what the owner has already said back. Both are bound to
                    # the content hash, because praise for a document that has
                    # since been rewritten is praise for the old one.
                    "quality": quality.get(deliverable["version_ref"]),
                    "feedback": {
                        "target_ref": deliverable["version_ref"],
                        "target_hash": deliverable["content_hash"],
                        "target_kind": "initial_screen",
                        "company_ref": company_ref,
                        # A Core with no journal table shows no buttons rather
                        # than buttons that fail when pressed. The card carried
                        # the flag at the top level and the binding did not, so
                        # the page had no way to act on it where the buttons
                        # actually are.
                        "enabled": journal["enabled"],
                        "entries": journal["by_target"].get(deliverable["version_ref"], []),
                    },
                }
            companies.append({
                "company_ref": company_ref, "ticker": member.get("ticker"), "name": COMPANY_NAMES.get(member.get("ticker", ""), ""),
                "priority": member.get("bootstrap_priority"), "tier": member.get("coverage_tier"),
                "stage": entry["stage_label"], "stage_ref": entry["stage"],
                "stage_status": entry["stage_status_label"], "note": note,
                "checklist": entry["items"],
                "document": deliverable,
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
                # P11a: the last close, when it is from, and whether the day
                # it belongs to had actually finished when it was read.
                "market": market.get(company_ref),
                # P11c: the four multiples with their percentiles and the
                # basis each percentile rests on.
                "valuation": valuation.get(company_ref),
                # P13-M2: how much of this company's forecast model stands up,
                # counted rather than scored.
                "model": forecasts.get(company_ref),
                # Q1: what the PM has said about this company's work so far.
                "feedback": journal["by_company"].get(company_ref),
                # P14a 今日事件: what happened to this company, by kind, each
                # carrying the tier a reader should believe it at.
                "events": events.get(company_ref),
                # P14a 大脑的判断: the decision word, the one-line because,
                # what the effect actually was, and what the independent
                # reader said about it.
                "judgements": judgements["by_company"].get(company_ref),
                # P14a 反思: what we expected, what happened, what debate we
                # may have missed, and what we would follow up.
                "reflections": reflections["by_company"].get(company_ref),
                # C1 下一个催化剂 T−N 天, with 日期未确认 beside it when the
                # date is a vendor's guess rather than the company's word.
                "catalyst": catalysts.get(company_ref),
                # P14a 频率: how often we look at each source, and why. The
                # policy baseline for every source, replaced by the brain's
                # own version where it has published one.
                "cadence": cadences.get(company_ref) or cadences.get("__baseline__"),
                # P14e 专项研究: what is being researched, on what budget,
                # and what it concluded or is still missing.
                "research_tasks": tasks.get(company_ref),
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
            # C2: one total cannot tell "the system stopped" from "the cheap
            # half of the system stopped". Four pools can.
            "pools": self._pools(mission, today),
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
                # P12c: the cap comes from the mission budget, not from the
                # role text. The role is prose the owner wrote once; live it
                # still said "24h/30 次上限" long after the cap became 130, and
                # the page was faithfully showing a number that had been wrong
                # for days. Prose describes the source; the budget is the cap.
                "sources": [{"source_ref": s["source_ref"], "label": SOURCE_LABELS.get(s["source_ref"], s["source_ref"]),
                             "role": s["role"], "connected": s["status"] == "connected",
                             "daily_cap": _source_daily_cap(s["source_ref"], mission["budget"])}
                            for s in mission["source_plan"]],
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
                "lanes": self._lane_states(heartbeat, extraction, discovery, mission["budget"], planner),
                "running": running,
                # C2: how many heartbeats had nothing to do, and which lane
                # could not work. Q2 found this unanswerable because the
                # driver's summary was overwritten every tick; it now has a
                # ledger, so the page can say it.
                "ticks": self._ticks(),
            },
            # P13w: the system's own decision about what to work on next. Top
            # level, beside the goal it serves -- it is not an activity note.
            "plan": plan,
            "budgets": budgets,
            # Q1: whether this Core can take the feedback buttons at all. A
            # Core with no journal table shows no buttons rather than buttons
            # that fail when pressed.
            "feedback_enabled": journal["enabled"],
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
                # Bound by hash as well as ref: a verdict about a document
                # that has since been rewritten is a verdict about the old one.
                "content_hash": record["content_hash"],
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
            # INT1: the reason a gate passed or failed lives inside the stage
            # record, not in a column. Selecting it as one made this whole page
            # raise "no such column: rationale" the first time a deliverable
            # existed to open -- which is why nothing had noticed.
            stage = [
                {"status": row["status"],
                 "rationale": json.loads(row["record_json"]).get("rationale"),
                 "at": row["created_at"]}
                for row in self._rows(core,
                    "SELECT status, record_json, created_at FROM coverage_mission_stage_records "
                    "WHERE mission_version_ref=? AND company_ref=? AND stage_ref='initial_screen' "
                    "ORDER BY created_at", (record["mission_version_ref"], record["subject_ref"]))
            ]
            claims = {claim["ref"]: claim for claim in self._claims(core)}
            quality = self._quality(core).get(ref)
            journal = self._journal(core)
        for section in record["sections"]:
            section["cited"] = [
                {"statement": claims[ref]["statement"], "period": claims[ref]["period"]}
                for ref in section["claim_refs"] if ref in claims
            ]
        return {
            **record, "company": self._label(members, record["subject_ref"]),
            "stage_history": stage, "as_of": _iso(self.clock()),
            # Q1: the rubric's reading of this exact version, and the owner's
            # own verdicts on it, beside the text they are about.
            "quality": quality,
            "feedback": {
                "target_ref": ref, "target_hash": record["content_hash"],
                "target_kind": "initial_screen",
                "company_ref": record["subject_ref"],
                "enabled": journal["enabled"],
                "entries": journal["by_target"].get(ref, []),
            },
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

    def _budget_db(self) -> Path | None:
        """Where the day ledger lives, according to the model configuration."""

        if self.config.model_config_path is None:
            return None
        config = _load_json(self.config.model_config_path)
        if not isinstance(config, dict) or not config.get("budget_db"):
            return None
        return Path(str(config["budget_db"]))

    def _pools(self, mission: Mapping[str, Any], today: str) -> dict[str, Any] | None:
        """C2: what each of the four pools has left, and who ran out today.

        A day's cap split four ways is the difference between "the system
        stopped" and "the cheap half of the system stopped"; the owner cannot
        tell those apart from one total. Read from the day ledger, not from
        the Core, and empty on a ledger that has not been migrated -- a
        read-only copy from before C2 has no ``pool`` column at all.
        """

        path = self._budget_db()
        if path is None:
            return None
        from .budget_pools import POOL_NAMES, has_pool_columns, pool_status

        try:
            with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)) as ledger:
                ledger.row_factory = sqlite3.Row
                if not has_pool_columns(ledger):
                    return None
                status = pool_status(
                    ledger, mission=mission, day=today, now=self.clock())
        except sqlite3.Error:
            return None
        pools = []
        for name in POOL_NAMES:
            entry = status["pools"][name]
            pools.append({
                "pool": name, "label": POOL_LABELS.get(name, name),
                "cap_usd": round(entry["cap_micros"] / 1_000_000, 4),
                "spent_usd": round(entry["spent_micros"] / 1_000_000, 4),
                "remaining_usd": round(entry["remaining_micros"] / 1_000_000, 4),
                "borrowed_usd": round(entry["borrowed_micros"] / 1_000_000, 4),
                "borrowed_from": [POOL_LABELS.get(key, key)
                                  for key in entry["borrowed_from"]],
                "lent_usd": round(entry["lent_micros"] / 1_000_000, 4),
                "exhausted": entry["exhausted"],
                "note": ("这一池今天已经用完，剩下的请求会被拒" if entry["exhausted"]
                         else None),
            })
        return {
            "day": status["day"], "pools": pools,
            # A default split is not the owner's split. Saying so is the
            # difference between a number they chose and one they inherited.
            "caps_defaulted": status["caps_defaulted"],
            "caps_note": ("这四个上限是默认分法，研究目标里没有自己的分法"
                          if status["caps_defaulted"] else "上限来自研究目标自己的分法"),
            "borrow_open": status["borrow_open"],
            "borrow_note": ("过了半天，闲着的池可以把额度借给覆盖池"
                            if status["borrow_open"] else "今天还早，暂时不允许互借"),
            "unpooled_usd": round(status["unpooled_micros"] / 1_000_000, 4),
            "exhausted_lane_count": status["exhausted_lane_count"],
            "exhausted_lanes": [
                {"lane": item["lane"],
                 "lane_label": REGISTRY_LANE_LABELS.get(item["lane"], item["lane"]),
                 "pool": item["pool"],
                 "pool_label": POOL_LABELS.get(item["pool"], item["pool"]),
                 "at": item["at"]}
                for item in status["exhausted_lanes"][:6]
            ],
        }

    def _ticks(self) -> dict[str, Any] | None:
        """C2: how many ticks ran, how many did nothing, and what stalled.

        Q2 found that this was unanswerable: the driver's summary went into
        ``heartbeat.json`` and the next tick overwrote it. It now has a
        ledger, so the answer exists and this is where the owner reads it.
        """

        from .tick_ledger import TickLedger, TickLedgerError, default_path

        path = default_path(self.config.state_dir)
        if not path.is_file():
            return None
        try:
            # Only the two summaries, never ``summarise``: that one also
            # returns every tick with every lane row, which is the whole
            # ledger on a page that refreshes.
            with TickLedger(path, read_only=True) as ledger:
                idle = ledger.idle_ratio()
                stalls = ledger.lane_stalls()
        except (TickLedgerError, sqlite3.Error, OSError, ValueError):
            return None
        if not idle.get("available"):
            return {"available": False, "reason": idle.get("reason"),
                    "window": idle.get("window")}
        stalled = [
            {"lane": key,
             "lane_label": REGISTRY_LANE_LABELS.get(key, key),
             "ticks": entry["ticks"], "stalls": entry["stalls"],
             "longest_stall_run": entry["longest_stall_run"],
             "pool_exhausted_ticks": entry["pool_exhausted_ticks"]}
            for key, entry in (stalls.get("lanes") or {}).items()
            if entry["stalls"] or entry["pool_exhausted_ticks"]
        ]
        stalled.sort(key=lambda row: -row["stalls"])
        return {
            "available": True, "window": idle.get("window"),
            "ticks": idle.get("ticks"), "idle_ticks": idle.get("idle_ticks"),
            "idle_ratio": idle.get("ratio"),
            "idle_note": (f"{idle.get('idle_ticks')}/{idle.get('ticks')} "
                          "次心跳里没有任何流水线有事可做"),
            "stalled_lanes": stalled[:6],
        }

    def _lane_states(self, heartbeat: Mapping[str, Any], extraction: Mapping[str, Any],
                     discovery: Mapping[str, Any],
                     budget: Mapping[str, Any] | None = None,
                     planner: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        """The four named lanes with their budgets, then every registered one."""

        return [*self._base_lane_states(heartbeat, extraction, discovery, budget),
                *self._registry_lane_states(planner or {})]

    @staticmethod
    def _base_lane_states(heartbeat: Mapping[str, Any], extraction: Mapping[str, Any],
                          discovery: Mapping[str, Any],
                          budget: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        def one(key: str, label: str, status: str | None, note: str) -> dict[str, Any]:
            return {"key": key, "label": label, "status": status or "idle", "note": note}
        web = discovery.get("web_search") or {}
        web_status = (web.get("discovery") or {}).get("status") or (web.get("acquisition") or {}).get("status")
        ae_status = (discovery.get("discovery") or {}).get("status") or (discovery.get("acquisition") or {}).get("status")
        # P12d: the *effective* cap, which is the tighter of the mission
        # budget and this codebase's own owner cap. Showing the mission's
        # requested number would be the same mistake as the old literal, just
        # in the other direction: a number on the page that is not the number
        # the system is running.
        ae_cap = _effective_alphaengine_cap(discovery, budget)
        ae_note = _alphaengine_cap_note(ae_cap)
        awaiting = extraction.get("awaiting")
        last = extraction.get("last") or {}
        rows = [
            one("web", "搜索公开网页", web_status, "按公司轮流搜索并获取网页"),
            # P12c: the cap is read from the mission budget. It was a literal
            # "每 24 小时最多 30 次" here, so the page went on saying 30 for days
            # after the owner raised it to 130 -- a number on the owner's own
            # dashboard that no longer described the system.
            one("alphaengine", "获取研报与电话会", ae_status, ae_note),
            one("extraction", "阅读并提炼结论", extraction.get("status"),
                f"排队 {awaiting} 份" + (f"，上一轮读了 {len(last.get('drafted') or []) if isinstance(last.get('drafted'), list) else last.get('drafted', 0)} 段" if last else "")),
            one("weekly", "每周简报", (heartbeat.get("weekly_brief") or {}).get("state"), "每周四早上发到 Discord"),
        ]
        return rows

    def _lane_governance_record(self, spec: Any, context: Any) -> str | None:
        """The connector record this lane is switched on by, if it has one.

        Read out of the lane's own LaunchAgent fragment rather than from a
        table here, because that fragment is already the single place a lane
        says what it needs installed -- the installer and the plist both
        derive from it, and a second list in the cockpit would be a third
        opinion that goes stale on its own schedule.
        """

        if spec.argv_fragment is None:
            return None
        try:
            argv = spec.argv_fragment(context)
        except Exception:  # noqa: BLE001 - a lane's fragment is not the page's problem
            return None
        for value in argv:
            if isinstance(value, str) and "connector-governance" in value:
                return Path(value).name
        return None

    def _registry_lane_states(self, planner: Mapping[str, Any]) -> list[dict[str, Any]]:
        """One row per registered lane, saying why a quiet one is quiet.

        P11a asked for this: a lane the mission never granted, a lane whose
        connector record is installed but unapproved, and a lane that is
        installed and simply has nothing to do all look identical on a page
        that only knows "idle". The first two are waiting on the owner and the
        third is not, and the difference is the whole reason to look.
        """

        from .lane_registry import LaunchAgentContext, registered_lanes

        governance = self._governance_records()
        context = LaunchAgentContext(state=self.config.state_dir)
        rows: list[dict[str, Any]] = []
        for spec in registered_lanes():
            key = spec.driver_key or spec.operation
            if key in LANES_SHOWN_ELSEWHERE:
                continue
            label = REGISTRY_LANE_LABELS.get(key, key)
            result = planner.get(key)
            if not isinstance(result, Mapping):
                rows.append({"key": f"lane:{key}", "label": label,
                             "status": "unstarted",
                             "note": LANE_STATUS_NOTES["unstarted"], "detail": None})
                continue
            status = str(result.get("status") or "idle")
            detail = str(result.get("reason") or "")
            note = LANE_STATUS_NOTES.get(status)
            record = self._lane_governance_record(spec, context)
            if record is not None and record in governance and governance[record] != "approved":
                # The record is on disk and the owner has not approved it --
                # or it is on disk and unreadable, which is not approval
                # either. The lane will keep starting children that refuse, so
                # the honest word is not "idle" and not "failed": it is
                # "waiting for you".
                status = "unapproved"
                note = f"数据源已经装好，等你批准（{record}）"
            if note is None:
                # A status this panel has no sentence for. Shown rather than
                # hidden, because a lane nobody can read about is the thing
                # this panel exists to stop -- but it is a gap here, not a
                # lane's fault, and the raw word is all there is to show.
                note = f"状态：{status}"
            skipped = result.get("skipped")
            if isinstance(skipped, list) and skipped:
                reasons = [str(item.get("reason")) for item in skipped
                           if isinstance(item, Mapping) and item.get("reason")]
                if reasons:
                    joined = "skipped: " + ", ".join(sorted(set(reasons))[:3])
                    detail = f"{detail}；{joined}" if detail else joined
            rows.append({"key": f"lane:{key}", "label": label, "status": status,
                         "note": note[:200], "detail": (detail[:300] or None),
                         "company_ref": result.get("company_ref")})
        return rows

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
            # P15d: a conviction call the machine proposed and nobody has
            # answered yet. Listed with no buttons on purpose: the decision op
            # exists on the writer, but the cockpit's own decide path and the
            # three buttons are integration work, and INT1's rule is that a
            # button which errors when pressed is worse than no button. What
            # the owner needs from this card today is to know the call is
            # waiting and what it says.
            for row in self._rows(core,
                "SELECT p.* FROM conviction_call_proposals p "
                "LEFT JOIN conviction_call_decisions d ON d.proposal_ref=p.proposal_id "
                "WHERE d.decision_id IS NULL ORDER BY p.created_at",
            ):
                record = json.loads(row["record_json"])
                variant = record.get("variant_view") or {}
                items.append({
                    "kind": "conviction_call", "ref": row["proposal_id"],
                    "hash": row["content_hash"], "at": row["created_at"],
                    "title": "是否采纳这条投资 call："
                             + CALL_DIRECTION_LABELS.get(row["direction"], row["direction"]),
                    "who": self._label(members, row["company_ref"]),
                    "summary": (variant.get("where_market_is_wrong") or {}).get("statement") or "",
                    "details": {
                        "我们的看法": (variant.get("our_view") or {}).get("statement"),
                        "市场的看法": (variant.get("market_view") or {}).get("statement")
                                      or (variant.get("market_view") or {}).get("reason"),
                        "时间跨度": CALL_HORIZON_LABELS.get(
                            row["time_horizon"], row["time_horizon"]),
                        "信心": row["confidence"],
                        "风险回报是否达标": CALL_STANDARD_LABELS.get(
                            row["risk_reward_status"], row["risk_reward_status"]),
                        "可观察信号": [step.get("signal") for step
                                       in record.get("event_pathway") or []],
                        "怎么裁决": "写者操作 decide_conviction_call（accept / reject / defer，要写理由）",
                    },
                    "actions": [],
                    "needs_rationale": False,
                })
        # INT2 / ADR-0007: the checkpoints the revision loop raises. Rendered
        # from whatever rows exist, with no decision buttons: the ops that
        # decide them are on another branch, and offering a button that goes
        # nowhere is worse than showing the item and saying who owes what.
        items.extend(self._revision_checkpoints())
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

    def _revision_checkpoints(self) -> list[dict[str, Any]]:
        """ADR-0007 / P14d: revision candidates, gate reopens, forecast overturns.

        Every one of these is a human checkpoint the mission vocabulary names
        and no lane may decide. Their rows may or may not be in this Core --
        the judgement lane writes the first and the third only when the
        mission grants them, and the fourth table does not exist yet -- so
        each is read only when its table is, and a decided one is filtered out
        only when a decisions table exists to filter against.

        **Whether it gets buttons is read off the Core, not assumed.** P14b and
        P14d added ``decide_thesis_revision_candidate`` and
        ``decide_gate_reopen`` and the two append-only decision ledgers those
        ops write to; a writer that carries the ops has opened those tables, so
        the presence of the decisions table is the honest, checkable test for
        "can this actually be decided from here". Where it is there, the row
        offers its two or three words and asks for a reason. Where it is not --
        an older Core, a state directory that predates this -- the row still
        appears, without buttons and saying who owes what, because a button
        that goes nowhere is worse than an item that says so.

        A candidate is shown with its reflection expanded rather than as a
        ref: ADR-0007's candidate and "what we may have missed" are worth
        exactly as much as each other when a person is deciding.
        """

        items: list[dict[str, Any]] = []
        with self._core() as core:
            mission = self._mission(core)
            members = self._members(mission)
            reflections = self._reflections(core)
            for kind, (table, key, decisions, ref_column) in CHECKPOINT_TABLES.items():
                if not _table_exists(core, table):
                    continue
                decidable = _table_exists(core, decisions)
                sql = f"SELECT t.* FROM {table} t "
                if decidable:
                    join = f"LEFT JOIN {decisions} d ON d.{ref_column}=t.{key} "
                    # ``defer`` is a decision that does not close the
                    # candidate, so on the full ledger only a terminal row
                    # takes it off the page. A ledger without the column is
                    # the older shape, where any row means decided.
                    if _column_exists(core, decisions, "terminal"):
                        join += "AND d.terminal=1 "
                    sql += join + "WHERE d.rowid IS NULL "
                for row in self._rows(core, sql + "ORDER BY t.created_at"):
                    record = json.loads(row["record_json"])
                    decision = record.get("decision")
                    details = {
                        "大脑的判断": JUDGEMENT_DECISION_LABELS.get(decision, decision),
                        "提议改成": record.get("proposed_statement"),
                        "提议的把握": record.get("proposed_confidence"),
                        "证伪条件": record.get("falsifier_ref"),
                        "依据": list(record.get("evidence_refs") or ()),
                    }
                    summary = record.get("because") or record.get("rationale") or ""
                    if kind == "gate_reopen":
                        summary, extra = _gate_reopen_view(record, summary)
                        details.update(extra)
                    items.append({
                        "kind": kind, "ref": row[key], "hash": row["content_hash"],
                        "at": row["created_at"],
                        "title": (CHECKPOINT_TITLES.get(kind) or kind),
                        "who": self._label(members, record.get("company_ref")),
                        "summary": summary,
                        "details": {name: value for name, value in details.items()
                                    if value not in (None, [], "")},
                        # Both halves, side by side.
                        "reflection": reflections["by_judgement"].get(
                            record.get("judgement_ref")),
                        "actions": list(CHECKPOINT_ACTIONS[kind]) if decidable else [],
                        "needs_rationale": decidable,
                        **({} if decidable else {
                            "note": CHECKPOINT_UNDECIDABLE_NOTES[kind]}),
                    })
            if _table_exists(core, "forecast_revision_proposals"):
                for row in self._rows(core,
                    "SELECT * FROM forecast_revision_proposals ORDER BY created_at",
                ):
                    record = json.loads(row["record_json"])
                    items.append({
                        "kind": "forecast_proposal", "ref": row["proposal_id"],
                        "hash": row["content_hash"], "at": row["created_at"],
                        "title": "预测行想改，但研究目标没有授权自动改",
                        "who": self._label(members, record.get("company_ref")),
                        "summary": record.get("because") or "",
                        "details": {
                            "哪条驱动": record.get("driver_ref"),
                            "哪一期": record.get("period_end"),
                            "想改成": record.get("proposed_value"),
                            "为什么没直接改": record.get("reason"),
                        },
                        "actions": [], "needs_rationale": False,
                        "note": "授予 forecast_line 之后判断层可以直接改，否则要人裁决",
                    })
        return items

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
        elif kind == "thesis_revision_candidate":
            # ADR-0007: automation may never take this branch. The cockpit
            # mints an ephemeral *human* principal for the call, and the
            # writer refuses the operation for anything else.
            if decision not in {"accept", "reject", "defer"}:
                raise CockpitError("decision must be accept, reject or defer")
            if not rationale.strip():
                raise CockpitError("请写一句理由")
            operation, params = "decide_thesis_revision_candidate", {
                "candidate_ref": ref, "candidate_hash": digest,
                "verdict": decision, "reason": rationale.strip()}
            title = {"accept": "接受了论点修订", "reject": "没有接受论点修订",
                     "defer": "把论点修订放了放"}[decision] + f"：{ref}"
        elif kind == "gate_reopen":
            if decision not in {"approve", "decline"}:
                raise CockpitError("decision must be approve or decline")
            if not rationale.strip():
                raise CockpitError("请写一句理由")
            operation, params = "decide_gate_reopen", {
                "proposal_ref": ref, "proposal_hash": digest,
                "verdict": decision, "reason": rationale.strip()}
            title = ("同意重出 Initial Screen" if decision == "approve"
                     else "不重出 Initial Screen") + f"：{ref}"
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

    # -- claims, models and feedback -----------------------------------------

    def claims(self, *, company_ref: str | None = None, index_aspect: str | None = None,
               importance: str | None = None, canonical_only: bool = True,
               limit: int = MAX_CLAIMS_IN_VIEW) -> dict[str, Any]:
        """The Ledger's conclusions, read through P12b's index.

        Canonical-only by default, which is what the index is for: the owner
        asking what is known about a company should get one copy of each fact
        rather than the same quarter's revenue three times. A claim the index
        has not reached yet is never hidden by that -- an unindexed claim is a
        gap in the index, not a reason to drop a fact.
        """

        from .claim_index_authority import table_exists
        from .company_research_view import (
            CompanyResearchViewValidationError, annotate_with_index,
        )

        if company_ref is not None:
            company_ref = _text(company_ref, "company_ref", maximum=512)
        with self._core() as core:
            mission = self._mission(core)
            members = self._members(mission)
            rows = self._claims(core)
            if company_ref is not None:
                rows = [row for row in rows if row["subject_ref"] == company_ref]
            indexed = table_exists(core)
            try:
                annotated = annotate_with_index(
                    core, rows, ref_key="ref", index_aspect=index_aspect,
                    importance=importance, canonical_only=canonical_only,
                )
            except CompanyResearchViewValidationError as exc:
                raise CockpitError(
                    "这个 Core 还没有给结论建索引，按主题或来源筛选在这里答不了"
                    if not indexed else f"筛选条件不对：{exc}"
                ) from exc
        # Most important first, then newest: a company report outranks a news
        # item about it, and among equals the recent one is the one to read.
        annotated.sort(key=lambda row: (row["index_order"], row["created_at"]))
        items = [{
            "ref": row["ref"], "statement": row["statement"],
            "company": self._label(members, row["subject_ref"]),
            "company_ref": row["subject_ref"], "period": row["period"],
            "at": row["created_at"],
            "aspect": row["index_aspect"],
            "aspect_label": ASPECT_LABELS.get(row["index_aspect"]),
            "importance": row["importance"],
            "importance_label": IMPORTANCE_LABELS.get(row["importance"]),
            "as_of": row["as_of"], "as_of_basis": row["as_of_basis"],
            "canonical": row["is_canonical"],
            # An untagged claim says so rather than looking like a tagged one
            # with nothing interesting in it.
            "indexed": row["index_aspect"] is not None,
        } for row in annotated[:max(1, min(int(limit), MAX_CLAIMS_IN_VIEW))]]
        return {
            "as_of": _iso(self.clock()), "indexed": indexed,
            "total": len(annotated), "items": items,
            "filters": {
                "company_ref": company_ref, "aspect": index_aspect,
                "importance": importance, "canonical_only": canonical_only,
            },
            "vocabulary": {
                "aspects": [{"value": key, "label": label}
                            for key, label in ASPECT_LABELS.items()],
                "importance": [{"value": key, "label": label}
                               for key, label in IMPORTANCE_LABELS.items()],
            },
            "companies": [{"company_ref": ref, "label": self._label(members, ref)}
                          for ref in members],
        }

    def company_model(self, company_ref: str) -> dict[str, Any]:
        """One company's forecast model, printed so a person can argue with it.

        The table is P13-M2's own renderer rather than a second layout here:
        it prints every assumption's ``because`` under the assumption and
        every unavailable result's reason where its number would be, and a
        cockpit-local re-rendering would lose exactly those two things.
        """

        ref = _text(company_ref, "company_ref", maximum=512)
        from .company_model_report import render_forecast_model
        from .model_forecast_driver import model_readiness

        with self._core() as core:
            if not _table_exists(core, "forecast_model_versions"):
                raise CockpitError("这个系统还没有开始建预测模型")
            mission = self._mission(core)
            members = self._members(mission)
            rows = self._rows(core,
                "SELECT record_json, version_number, created_at FROM forecast_model_versions "
                "WHERE company_ref=? ORDER BY version_number DESC", (ref,))
            if not rows:
                raise CockpitError("这家公司还没有预测模型")
            record = json.loads(rows[0]["record_json"])
            history = [{
                "version": row["version_number"], "created_at": row["created_at"],
                "change_reason": json.loads(row["record_json"]).get("change_reason"),
                "change_reason_label": CHANGE_REASON_LABELS.get(
                    json.loads(row["record_json"]).get("change_reason"),
                    json.loads(row["record_json"]).get("change_reason")),
            } for row in rows]
        label = self._label(members, ref)
        readiness = model_readiness(record)
        return {
            "as_of": _iso(self.clock()), "company_ref": ref, "company": label,
            "version": record.get("version"), "version_ref": record.get("id"),
            "created_at": record.get("created_at"),
            "change_reason": record.get("change_reason"),
            "change_reason_label": CHANGE_REASON_LABELS.get(
                record.get("change_reason"), record.get("change_reason")),
            "decision": record.get("decision"),
            "readiness": readiness, "note": self._forecast_note(readiness),
            "table": render_forecast_model(record, entity_name=label),
            "history": history,
        }

    # -- INT2: 来源 and 模型 --------------------------------------------------

    def sources(self) -> dict[str, Any]:
        """P14a's SourceCapabilityMap and P14-M's routing, on one page.

        The owner asked for the first one by name: the brain has to know what
        each connector can actually hand over before it can decide where to
        go for something. The second is the same question about the model
        side -- which model serves a purpose, what happens when it is down,
        and whether the catalog this Core holds is the catalog the broker
        offers.
        """

        from .source_capability_map import build_map
        from .tracking_cadence import baseline_cadences

        policy = self._tracking_policy()
        with self._core() as core:
            mission = self._mission(core)
        projection = build_map(
            mission=mission,
            cadences=None if policy is None else baseline_cadences(policy),
        )
        rows = []
        for entry in projection["sources"]:
            status = entry["connection_status"]
            tier = entry["evidence_tier"]
            completeness = entry["completeness_ceiling"]
            rows.append({
                "slug": entry["slug"], "source_ref": entry["source_ref"],
                "label": SOURCE_LABELS.get(entry["source_ref"], entry["slug"]),
                # 能取什么
                "content": [CONTENT_KIND_LABELS.get(kind, kind)
                            for kind in entry["content_kinds"]],
                # 层级
                "tier": tier, "tier_label": EVIDENCE_TIER_LABELS.get(tier, tier),
                # 状态
                "status": status,
                "status_label": CONNECTION_STATUS_LABELS.get(status, status),
                "installed": entry["in_inventory"],
                "installed_note": (None if entry["in_inventory"]
                                   else "这条来源还没装进目录"),
                # 配额: the map carries each row as sorted item pairs so the
                # projection can be hashed; a dict is what a page renders.
                "quotas": [
                    {"operation": row.get("operation"),
                     "daily_limit": row.get("daily_unit_limit"),
                     "unit": row.get("quota_unit")}
                    for row in (dict(pairs) for pairs in entry["quotas"])
                ],
                # 频率
                "cadence_label": _interval_label(entry["cadence_baseline_seconds"]),
                "cadence_adjustable": entry["cadence_adjustable"],
                "completeness": completeness,
                "completeness_label": COMPLETENESS_LABELS.get(completeness),
                # web fetch / web search answer anything, which is what makes
                # them the last resort rather than the first: the owner asked
                # for the generic ones to be marked as such.
                "generic": entry["generic"],
                "generic_note": ("通用来源：什么都能问，所以只在没有专门来源时才用"
                                 if entry["generic"] else None),
                "markets": list(entry["markets"]),
                "note": entry["note"],
            })
        # Specific sources first, generic last, exactly the order the map's
        # own ``sources_for`` hands them to a research decision.
        rows.sort(key=lambda row: (row["generic"], row["slug"]))
        return {
            "as_of": _iso(self.clock()),
            "schema_version": SCHEMA_VERSION,
            "sources": rows,
            "content_kinds": [{"value": kind, "label": CONTENT_KIND_LABELS.get(kind, kind)}
                              for kind in projection["content_kinds"]],
            "policy_ref": None if policy is None else policy["policy_ref"],
            "policy_note": (None if policy is not None else
                            "这台机器上还没有跟踪频率政策，所以频率一栏是空的"),
            "routing": self._routing(),
        }

    def _routing(self) -> dict[str, Any]:
        """P14-M: which model serves each purpose, and what it falls back to."""

        config = (_load_json(self.config.model_config_path)
                  if self.config.model_config_path is not None else None)
        path = (config or {}).get("model_router_db") if isinstance(config, dict) else None
        if not path or not Path(str(path)).is_file():
            return {"available": False,
                    "reason": "这台机器上还没有模型路由库，所以没有可读的路由"}
        from .model_fallback_chain import FallbackChainError, routing_overview
        from .model_router import ModelRouter

        broker = (None if self.config.openclaw_config_path is None
                  else _load_json(self.config.openclaw_config_path))
        try:
            with closing(ModelRouter(str(path), read_only=True)) as router:
                overview = routing_overview(
                    router,
                    openclaw_config=broker if isinstance(broker, Mapping) else None,
                    checked_at=self.clock(),
                )
        except (FallbackChainError, sqlite3.Error, OSError, ValueError) as exc:
            return {"available": False, "reason": f"路由库读不出来：{_reason(exc)}"}
        tiers = []
        for tier, entry in overview["tiers"].items():
            served = entry["last_served"]
            tiers.append({
                "tier": tier, "label": MODEL_TIER_LABELS.get(tier, tier),
                # The chain in order, so "what runs when the first one is
                # down" is a thing on the page rather than a thing to ask.
                "chain": [{
                    "position": link["position"], "model": link["profile_id"],
                    "registered": link["registered"], "status": link["status"],
                    "family": link["family"],
                    "note": (None if link["registered"]
                             else "这台机器上没有这个模型的档案"),
                } for link in entry["chain"]],
                "last_served": None if served is None else {
                    "model": served["profile_id"],
                    "position": served["chain_position"],
                    "purpose": served["purpose"],
                    "at": served["created_at"],
                },
                "last_served_note": (
                    "还没有用过这一层" if served is None else
                    (f"上一次是链上第 {served['chain_position']} 个模型服务的"
                     + ("（也就是第一选择）" if served["chain_position"] == 1
                        else "——第一选择当时没答上"))),
                "skipped_since_last_served": [
                    {"model": link["profile_id"], "reason": link["skip_reason"]}
                    for link in entry["skipped_since_last_served"][-4:]
                ],
            })
        catalog = overview["catalog"]
        return {
            "available": True,
            "purposes": [
                {"purpose": purpose, "tier": tier,
                 "tier_label": MODEL_TIER_LABELS.get(tier, tier)}
                for purpose, tier in sorted(overview["purpose_tiers"].items())
            ],
            # A purpose with no tier is refused rather than defaulted, so an
            # unmapped one is a lane that cannot call a model at all.
            "unmapped_purposes": list(overview["unmapped_purposes"]),
            "unmapped_note": (None if not overview["unmapped_purposes"] else
                              "这些用途还没有指定层级，它们一次模型都调不了"),
            "tiers": tiers,
            "catalog": None if catalog is None else {
                "in_sync": catalog["catalog_in_sync"],
                "checked_at": catalog["checked_at"],
                "note": ("这台机器的模型目录和网关一致" if catalog["catalog_in_sync"]
                         else "这台机器的模型目录和网关不一致，跑一次安装脚本会对上"),
                # Both diff sets by name: "which way" is the only part of
                # "out of sync" that tells anybody what to do.
                "missing_here": list(catalog["missing_static_profile_ids"]),
                "not_in_broker": list(catalog["not_in_broker_profile_ids"]),
            },
            "catalog_note": (None if catalog is not None else
                             "没有配置 OpenClaw 网关的位置，所以无法比对模型目录"),
        }

    def cycle_reflection(self) -> dict[str, Any]:
        """Q2: 每周回头看 -- the latest week's "我们把时间花在哪"."""

        with self._core() as core:
            mission = self._mission(core)
            if not _table_exists(core, "research_cycle_reflection_versions"):
                return {"as_of": _iso(self.clock()), "available": False,
                        "reason": "这个 Core 还没有写过每周回头看"}
            rows = self._rows(core,
                "SELECT record_json, iso_week, created_at FROM "
                "research_cycle_reflection_versions WHERE mission_ref=? "
                "ORDER BY iso_week DESC, version_number DESC LIMIT 1",
                (mission["mission_ref"],))
            weeks = [row["iso_week"] for row in self._rows(core,
                "SELECT DISTINCT iso_week FROM research_cycle_reflection_versions "
                "WHERE mission_ref=? ORDER BY iso_week DESC LIMIT 12",
                (mission["mission_ref"],))]
        if not rows:
            return {"as_of": _iso(self.clock()), "available": False,
                    "reason": "这个研究目标还没有写过每周回头看"}
        record = json.loads(rows[0]["record_json"])
        narrative = record.get("narrative") or {}
        return {
            "as_of": _iso(self.clock()), "available": True,
            "iso_week": record.get("iso_week"),
            "window": {"since": record.get("window_start"),
                       "until": record.get("window_end")},
            "written_at": rows[0]["created_at"],
            "version": record.get("version"),
            "title": narrative.get("title") or "我们把时间花在哪",
            # The prose and the table are both derived from the metrics; no
            # model wrote either, which is why they can be shown verbatim.
            "prose": narrative.get("prose"),
            "table": [dict(row) for row in narrative.get("table") or ()],
            "backlog_candidates": [
                {"question": item.get("question"), "because": item.get("because"),
                 "refs": list(item.get("refs") or ())}
                for item in record.get("backlog_candidates") or ()
            ],
            # These are sentences for the owner, not decisions: the reflection
            # has no path to change a policy and says so in its own record.
            "policy_suggestions": list(record.get("policy_suggestions") or ()),
            "authority_note": record.get("authority_note"),
            "weeks": weeks,
        }

    def record_feedback(self, login: str, value: Mapping[str, Any]) -> dict[str, Any]:
        """Q1: one PM verdict, written through the writer as the owner.

        The cockpit holds no Core write handle, so this goes out as the
        owner's Tailscale-derived principal through the same ephemeral
        governance path every other cockpit decision takes. Nothing here is a
        new authority: the journal refuses a non-human principal on its own.
        """

        if not isinstance(value, Mapping):
            raise CockpitError("feedback must be an object")
        target_ref = _text(value.get("target_ref"), "target_ref", maximum=512)
        target_hash = _sha(value.get("target_hash"), "target_hash")
        target_kind = _text(value.get("target_kind"), "target_kind", maximum=64)
        verdict = _text(value.get("verdict"), "verdict", maximum=32)
        # Checked against the authority's own vocabularies, not against the
        # label map: a word with no Chinese label would be a display bug, and
        # a word the journal does not take is a refusal. Both are checked here
        # so that a typo in the page costs a message rather than an ephemeral
        # human principal and a round trip to the writer.
        from .analyst_journal import TARGET_KINDS, VERDICTS

        if verdict not in VERDICTS:
            raise CockpitError("这不是一个可以给的反馈")
        if target_kind not in TARGET_KINDS:
            raise CockpitError("这不是一种可以给反馈的产出")
        note = value.get("note") or None
        if note is not None:
            note = _text(note, "note", maximum=4000)
        company_ref = value.get("company_ref") or None
        if company_ref is not None:
            company_ref = _text(company_ref, "company_ref", maximum=512)
        actor = _subject_for_login(login)
        params = {
            "target_ref": target_ref, "target_hash": target_hash,
            "target_kind": target_kind, "verdict": verdict,
            # Content-addressed rather than "cockpit:<login>:...": the login is
            # an email address and the actor is deliberately a hash of it, so
            # spelling the address into the Core's idempotency key would undo
            # that. It is also the only form that stays inside the key's own
            # length limit whatever the target ref looks like.
            "idempotency_key": "cockpit:" + content_hash({
                "actor": actor, "target": target_ref, "verdict": verdict})[:32],
        }
        if note:
            params["note"] = note
        if company_ref:
            params["company_ref"] = company_ref
        try:
            result = self.governance_call(
                self.token_config, self.writer_socket, actor_ref=actor,
                operation="record_analyst_journal_entry", params=params)
        except RemoteError as exc:
            # A refusal by contract is the caller's mistake and reads as 400;
            # a conflict is about what is already stored and reads as 409.
            # Answering "conflict" to a malformed request tells the page to
            # offer a retry that will fail the same way forever.
            if getattr(exc, "code", None) in {"rejected", "protocol_error", "forbidden"}:
                raise CockpitError(f"这条反馈没有被记下：{_reason(exc)}") from exc
            raise CockpitConflict(f"这条反馈没有被记下：{_reason(exc)}") from exc
        except GovernanceCliError as exc:
            raise CockpitConflict(f"这条反馈没有被记下：{_reason(exc)}") from exc
        label = VERDICT_LABELS.get(verdict, verdict)
        self.journal.record_event(
            kind="feedback", title=f"你对一份产出说了「{label}」", detail=note,
            login=login, refs={"target_ref": target_ref, "verdict": verdict})
        status = result.get("status") if isinstance(result, Mapping) else None
        return {"status": status or "recorded", "verdict": verdict,
                "verdict_label": label, "target_ref": target_ref,
                "duplicate": status == "duplicate"}

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
        # P15a: the owner asking for the one bounded look is a second click,
        # never a default.  A panel that searched whenever the model said it
        # would like to would spend the mission's connector quota on curiosity.
        refresh = bool(value.get("refresh"))
        model = self._model_instance()
        return self._start_job("ask", login, {"question": question, "request_id": request_id,
                                              "refresh": refresh},
                               lambda: self._answer(model, login, question, request_id,
                                                    refresh=refresh))

    def _indexed_claims(self, core: Any, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The answer context, read through P12b's index.

        The cockpit reads ``claim_versions`` directly rather than through the
        projection, so before this it saw every copy of every fact: the same
        quarter's revenue from the filing, from a broker note and from a news
        story, and an answer that cited all three read as three sources
        agreeing. Canonical-only is the fix, and it only ever drops a row the
        index has positively marked as a duplicate of another -- an untagged
        claim is a gap in the index, never a reason to hide a fact.

        The order is left as it was, oldest first: which claims survive the
        prompt budget is decided by recency, and quietly re-sorting by
        importance here would change which ones the model ever sees.
        """

        from .company_research_view import annotate_with_index

        return annotate_with_index(core, rows, ref_key="ref")

    def _answer_policy(self, core: Any, company_ref: str | None) -> dict[str, Any]:
        """The live answer-sufficiency policy, as the router itself reads it.

        Not re-implemented here.  ``answer_routing.active_policy_for_company``
        is the router's own projection lifted to a module function: it resolves
        the active mandate, checks the pointer hash, checks that the policy was
        written against *this* version of the mandate, and checks the effective
        window.  An earlier draft of this method asked only "is there a pointer
        row", which would have let a question spend the mission's connector
        quota under a policy the owner had already superseded.
        """

        from .answer_routing import active_policy_for_company

        return active_policy_for_company(
            core, company_ref, as_of=_iso(self.clock()))

    def _already_refreshed(self, request_id: str) -> bool:
        """Whether this exact question has already spent its one look."""

        rows = self.journal.rows(
            "SELECT refs_json FROM cockpit_events WHERE kind='refresh'")
        return any(json.loads(row["refs_json"]).get("request_id") == request_id
                   for row in rows)

    def _answer(self, model: CockpitModel, login: str, question: str, request_id: str,
                *, refresh: bool = False) -> dict[str, Any]:
        from . import ask_answer, ask_context, ask_refresh

        today = _iso(self.clock())[:10]
        with self._core() as core:
            mission = self._mission(core)
            everything = self._claims(core)
            claims = self._indexed_claims(core, everything)
            theses = [json.loads(r["content_json"]) for r in core.execute(
                "SELECT content_json FROM thesis_versions ORDER BY created_at").fetchall()]
            journal_enabled = _table_exists(core, "analyst_journal_entries")
            members = self._members(mission)
            context = ask_context.build_context(
                core, question=question, mission=mission, members=members,
                claims=claims, theses=theses, company_names=COMPANY_NAMES,
                label=lambda ref: self._label(members, ref),
                today=today,
                duplicates_dropped=len(everything) - len(claims),
            )
            # The policy governs a company, so it is read after the question's
            # subjects are resolved rather than before.
            named = list(context["subjects"]["companies"])
            policy_state = self._answer_policy(core, named[0] if named else None)
            specs = ask_refresh.known_specs(core, mission["mission_ref"])
            pool = ask_refresh.pool_balance(core, mission, day=today)
        prompt = ask_answer.build_prompt(context, mission=mission)
        call = model.call(purpose="ask", request_id=request_id, prompt=prompt, mission=mission)
        answer = ask_answer.parse_answer(unwrap_json_object(call["text"]) or {}, context=context)
        cost_micros = call["cost_micros"]
        replayed = call["replayed"]

        plan = ask_refresh.plan_refresh(
            answer, context, mission=mission,
            policy=policy_state["policy"] if policy_state["state"] == "active" else None,
            specs=specs, pool=pool,
            already_refreshed=self._already_refreshed(request_id))
        outcome: dict[str, Any] | None = None
        if refresh and plan["available"]:
            outcome = ask_refresh.run_refresh(
                plan,
                start=lambda **params: self._start_discovery(login, **params),
                status=lambda ticket_ref: self._discovery_status(login, ticket_ref),
                documents=lambda discovery_ref: self._discovered_by(
                    mission["mission_ref"], discovery_ref))
            self.journal.record_event(
                kind="refresh",
                title=f"为回答你的问题补搜了一次：{plan['suggestion']['source']}",
                detail=outcome["status_label"], login=login,
                refs={"request_id": request_id, "status": outcome["status"],
                      "ticket_ref": outcome["ticket_ref"], **outcome["operation"]})
            plan = {**plan, "available": False, "reasons": ["already_refreshed"],
                    "reason_labels": [ask_refresh.GRANT_LABELS["already_refreshed"]]}
            if outcome["headers"]:
                # The second and last call.  A new request id, because it is a
                # different prompt and the scheduler is content-addressed on
                # it; the same day ledger, because it is the same mission's
                # money. Nothing found means nothing new to read, so the first
                # answer stands and the owner is told the search came back
                # empty rather than charged for a second identical call.
                again = ask_refresh.with_headers(context, outcome["headers"])
                second = model.call(purpose="ask", request_id=f"{request_id}:refresh",
                                    prompt=ask_answer.build_prompt(again, mission=mission),
                                    mission=mission)
                answer = ask_answer.parse_answer(
                    unwrap_json_object(second["text"]) or {}, context=again)
                context = again
                cost_micros += second["cost_micros"]
                replayed = replayed and second["replayed"]

        shown = [dict(row) for row in context["shown"]]
        result = {
            "question": question,
            "answer": answer["answer"],
            "sentences": answer["sentences"],
            # The page's existing citation card reads ``statement``,
            # ``company``, ``period`` and ``at``; those four keep their names
            # so that an answer citing a valuation row renders in the card
            # that already exists. ``block`` and ``block_label`` are what let
            # it say which kind of thing was cited.
            "citations": [{
                "tag": row["tag"], "statement": row["statement"], "ref": row["ref"],
                "period": row["period"], "company": row.get("company") or "",
                "at": row.get("at") or "", "block": row["block"],
                "block_label": ask_context.BLOCK_LABELS[row["block"]],
            } for row in answer["citations"]],
            "gaps": answer["gaps"],
            "unknowns": answer["unknowns"],
            "confidence": answer["confidence"],
            "refused": answer["refused"],
            "refusal_reason": answer["refusal_reason"],
            "refusal_label": answer["refusal_label"],
            "refusal_detail": answer["refusal_detail"],
            "market_vs_us": answer["market_vs_us"],
            "verification": answer["verification"],
            "context": {
                "question_kind": context["question_kind"],
                "wants_market_vs_us": context["wants_market_vs_us"],
                "companies": context["companies"],
                "blocks": [{"block": b["block"], "label": b["label"],
                            "available": b["available"], "reason": b["reason"],
                            "rows": len(b["rows"])} for b in context["blocks"]],
                "missing": context["missing"],
                "context_hash": context["context_hash"],
                "budget_chars": context["budget_chars"],
                "spent_chars": context["spent_chars"],
            },
            # Why the answer policy did or did not govern this question, in the
            # router's own three words. A refresh shut because the policy is
            # ``stale`` is a different fix from one shut because there is none.
            "answer_policy": {"state": policy_state["state"],
                              "reason": policy_state["reason"],
                              "mandate_ref": policy_state["mandate_ref"]},
            "refresh": {
                "available": plan["available"],
                "reasons": plan["reasons"], "reason_labels": plan["reason_labels"],
                "suggestion": plan["suggestion"],
                # True whenever a search actually ran, including the run that
                # found nothing: "we looked and there was nothing" is not the
                # same answer as "we did not look".
                "ran": bool(outcome and outcome["ran"]),
                "status": None if outcome is None else outcome["status"],
                "status_label": None if outcome is None else outcome["status_label"],
                "ticket_ref": None if outcome is None else outcome["ticket_ref"],
                "discovery_ref": None if outcome is None else outcome["discovery_ref"],
            },
            "refreshed_with": [] if outcome is None else outcome["headers"],
            "refresh_note": None if outcome is None else outcome["note"],
            "claims_considered": context["claims_considered"],
            "claims_total": len(everything),
            # How many copies of a fact the index took out before the
            # model saw them. Shown because "we read 400 conclusions"
            # and "we read 400 conclusions, 90 of them the same fact
            # three times" are different statements about an answer.
            "duplicates_dropped": len(everything) - len(claims),
            "cost_usd": round(cost_micros / 1_000_000, 4),
            "replayed": replayed, "answered_at": _iso(self.clock()),
        }
        # Q1: the answer is a cockpit artifact with no Core record, so the
        # thing a verdict binds to is a hash of what was said and what it
        # cited. Keyed on the request rather than the job, so feedback on a
        # replayed answer lands on the same answer instead of splitting.
        from .research_quality_rubrics import rubric as get_rubric
        from .research_quality_score import artefact_from_ask_answer, run_deterministic

        artefact = artefact_from_ask_answer(
            result, shown_claims=shown, ref=f"cockpit-ask:{request_id}")
        result["feedback"] = {"target_ref": artefact["ref"], "target_hash": artefact["hash"],
                              "target_kind": "ask_answer", "enabled": journal_enabled}
        # P15a: Q1's deterministic layer runs on every answer, not only on the
        # ones somebody later thinks to grade, and its result rides with the
        # answer. It is a cockpit artefact like the answer: nothing is recorded
        # to the Core, and no score authority is touched from this process.
        rubric = get_rubric("ask_answer")
        result["quality"] = {
            "rubric_ref": rubric.rubric_ref, "rubric": rubric.title,
            "recorded": False,
            **run_deterministic(artefact, rubric),
        }
        self.journal.record_event(kind="question", title=f"你问了：{question[:120]}",
                                  detail=answer["answer"][:300], login=login,
                                  refs={"job_kind": "ask", "request_id": request_id,
                                        "work_order_ref": call["work_order_ref"]})
        return result

    # -- the one bounded look, through the writer -------------------------------------------

    def _start_discovery(self, login: str, *, company_ref: str, source_ref: str,
                         spec_ref: str) -> dict[str, Any]:
        """Spawn one governed discovery, as the owner themselves.

        The cockpit process holds no Core write handle (ADR-0006) and no
        connector credential; this is the same governance path every other
        cockpit write takes, under the owner's own Tailscale-derived principal,
        and the writer re-derives the mission grant before a byte moves. A
        human requester is also what lets a ``probe_only`` source be searched,
        which is how the owner rehearses a connector.
        """

        actor = _subject_for_login(login)
        return self._discovery_call(
            actor, "run_mission_source_discovery",
            {"company_ref": company_ref, "source_ref": source_ref,
             "spec_ref": spec_ref, "requested_by": actor})

    def _discovery_status(self, login: str, ticket_ref: str) -> dict[str, Any]:
        return self._discovery_call(
            _subject_for_login(login), "mission_source_discovery_status",
            {"ticket_ref": ticket_ref})

    def _discovery_call(self, actor: str, operation: str,
                        params: Mapping[str, Any]) -> dict[str, Any]:
        try:
            result = self.governance_call(
                self.token_config, self.writer_socket, actor_ref=actor,
                operation=operation, params=dict(params))
        except (GovernanceCliError, RemoteError) as exc:
            raise CockpitConflict(f"这次补搜没有跑成：{_reason(exc)}") from exc
        return dict(result) if isinstance(result, Mapping) else {}

    def _discovered_by(self, mission_ref: str, discovery_ref: str) -> list[dict[str, Any]]:
        """Exactly the documents one discovery recorded, newest search first.

        Read from the discovery record's own ``document_refs`` rather than from
        "this company's documents": the second cannot tell what this search
        returned from what was already in the ledger, and an answer that showed
        the difference as "just fetched" would be lying in the most convincing
        possible way. Scoped by ``mission_ref`` rather than by the active
        mission version, because publishing a version must not hide the rows
        the previous one discovered.
        """

        with self._core() as core:
            if not _table_exists(core, "coverage_mission_source_discoveries"):
                return []
            row = core.execute(
                "SELECT d.record_json AS record_json FROM "
                "coverage_mission_source_discoveries d "
                "JOIN coverage_mission_versions v "
                "ON v.mission_version_id=d.mission_version_ref "
                "WHERE d.record_id=? AND v.mission_ref=?",
                (discovery_ref, mission_ref),
            ).fetchone()
            if row is None:
                return []
            record = json.loads(row["record_json"])
            refs = [str(ref) for ref in record.get("document_refs") or ()]
            fresh = {str(ref) for ref in record.get("new_document_refs") or ()}
            if not refs:
                return []
            marks = ",".join("?" for _ in refs)
            rows = {}
            if _table_exists(core, "coverage_mission_discovered_documents"):
                for item in core.execute(
                    f"SELECT document_ref, source_ref, host, status, created_at "
                    f"FROM coverage_mission_discovered_documents "
                    f"WHERE discovery_ref=? AND document_ref IN ({marks})",
                    (discovery_ref, *refs),
                ).fetchall():
                    rows[item["document_ref"]] = dict(item)
        titles = self._url_map()
        out = []
        for ref in refs:
            stored = rows.get(ref, {})
            known = titles.get(ref) or {}
            out.append({
                "document_ref": ref,
                "source_ref": stored.get("source_ref") or record.get("source_ref"),
                "host": stored.get("host") or known.get("host"),
                "title": known.get("title"),
                "status": stored.get("status") or "discovered",
                "created_at": stored.get("created_at"),
                "new": ref in fresh,
            })
        return out

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
