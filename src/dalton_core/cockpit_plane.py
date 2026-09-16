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
import base64
import binascii
import hashlib
import os
import re
import secrets
import sqlite3
import threading
import time
from collections.abc import Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .cockpit_model import CockpitModel, CockpitModelError, unwrap_json_object
from .final_text_contract import final_text_instructions
from .claim_retirement import REASON_LABELS as CLAIM_REASON_LABELS
from .coverage_mission import STAGE_REOPENED
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
    "source:company-ir": "公司官网投资者关系页面", "source:guidepoint": "专家访谈", "source:web-search": "公开网页搜索",
    "source:sales-notes": "卖方销售快报", "source:company-wiki": "公司知识库",
    "source:prior-research": "历史研究资料",
    # S3 crowd sources, named like the rest rather than by their refs.
    "source:xueqiu": "雪球散户讨论", "source:x": "X（推特）公开讨论",
    "source:blind": "匿名员工评价（Blind）",
}
SOURCE_SLUG_LABELS = {
    "alphaengine": "卖方研报与电话会", "catalyst-calendar": "催化剂日历",
    "cn-hk-findata": "沪深港财务与交易数据", "cninfo": "巨潮资讯",
    "company-wiki": "公司知识库", "employee-reviews": "员工评价",
    "gemini-web-search": "Gemini 公开网页搜索", "guidepoint": "Guidepoint 专家访谈",
    "hkex-filings": "香港交易所公告", "reddit-last30days": "Reddit 近 30 天讨论",
    "roic-transcript": "ROIC 电话会纪要", "sales-notes": "卖方销售快报",
    "sec": "SEC 财报与公告", "sec-financials": "SEC 三张财务报表",
    "sec-ownership": "SEC 股东与高管持股申报", "web-fetch": "公开网页读取",
    "x-x-search": "X 站内搜索", "x-xreach": "X 动态与新闻",
    "xueqiu": "雪球散户讨论", "x-xreach": "X（推特）公开讨论",
    "employee-reviews": "匿名员工评价（Blind）",
    "yfinance": "Yahoo Finance 市场数据",
}


def _stage_readiness_labels(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Keep an immutable gate decision separate from today's source base."""

    source_ready = bool(entry.get("source_base_ready"))
    gate_status = entry.get("stage_status")
    gate_label = {
        None: "尚无审批记录", "entered": "已进入，等待审批",
        "gate_passed": "历史检查已通过", "gate_failed": "历史检查未通过",
    }.get(gate_status, f"此前审批：{entry.get('stage_status_label') or gate_status}")
    readiness_label = "当前资料已齐" if source_ready else "当前资料待补齐"
    result = {
        "journey_status": (f"{gate_label} · {readiness_label}"
                           if entry.get("stage") in {None, "initial_screen"}
                           else gate_label),
        "gate_decision": {"stage_ref": entry.get("stage"),
                          "status": gate_status, "label": gate_label},
        "source_readiness": {
            "scope": "initial_screen", "ready": source_ready,
            "status": "ready" if source_ready else "needs_material",
            "label": readiness_label, "gaps": list(entry.get("gaps") or ()),
            "blocked_on": list(entry.get("blocked_on") or ()),
        },
    }
    if entry.get("latest_generation_failure"):
        result["latest_generation_failure"] = entry["latest_generation_failure"]
    return result


def _initial_screen_failure_label(summary: Mapping[str, Any]) -> str | None:
    """Translate the latest failed generation into owner-facing language."""

    if summary.get("status") != "failed":
        return None
    reasons = " ".join(
        str(section.get("reason") or "")
        for section in summary.get("sections") or ()
        if isinstance(section, Mapping)
    )
    if ("MODEL_ROUTE_REJECTED" in reasons
            or "no model route is available right now" in reasons.casefold()):
        return "最新一次报告生成失败：模型服务或当时可用运行额度未能承接请求"
    reason = str(summary.get("failure_reason") or "").strip()
    return f"最新一次报告生成失败：{reason}" if reason else "最新一次报告生成失败"


def _latest_initial_screen_failures(state_dir: Path) -> dict[str, str]:
    latest: dict[str, tuple[str, str]] = {}
    root = state_dir / "initial-screens"
    if not root.is_dir():
        return {}
    for path in root.iterdir():
        ticket, summary = _load_json(path / "ticket.json"), _load_json(path / "summary.json")
        if not isinstance(ticket, dict) or not isinstance(summary, dict):
            continue
        company_ref = (summary.get("drafted") or {}).get("company_ref")
        label = _initial_screen_failure_label(summary)
        if not company_ref or not label:
            continue
        at = str(ticket.get("completed_at") or ticket.get("started_at") or "")
        if company_ref not in latest or at > latest[company_ref][0]:
            latest[company_ref] = (at, label)
    return {company_ref: value[1] for company_ref, value in latest.items()}
# P11x: what a figure is worth, in the owner's language. A number the company
# filed is its published figure; a number said on a call is a record of the
# saying. Both are kept; the label is how the difference stays visible.
_EMPTY_FIGURES: dict = {"total": 0, "by_grade": {}, "latest": []}
# What a directive asks for, in the owner's language.
PLAN_ACTION_LABELS = {
    "search": "查找资料", "acquire": "获取资料", "read": "阅读资料",
    "extract_figures": "提取财务数字", "stop": "停止",
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
    "read": "已审阅", "useful": "可用于研究判断", "needs_more_evidence": "仍需补充证据",
    "disagree": "不认同当前结论", "revise": "需要修订",
}
# The verdicts that say the last attempt was not enough.
OUTSTANDING_VERDICTS = frozenset({"needs_more_evidence", "disagree", "revise"})
IMPORTANCE_LABELS = {
    "filing": "公司报表原文", "management_statement": "管理层直接表述",
    "sell_side": "卖方观点", "news": "新闻报道", "other": "其他",
}
ASPECT_LABELS = {
    "business_model": "商业模式与盈利来源", "segments_and_mix": "业务构成",
    "demand_drivers": "需求驱动因素", "supply_and_cost": "成本与供给",
    "competitive_position": "竞争地位",
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
    "not_met": "未达到手册规定的风险回报标准",
    "unavailable": "缺少可供对照的数字",
    "not_applicable": "手册未规定这类投资判断的回报标准",
}
# P11c asked for this one specifically: a percentile computed while the
# fundamentals never moved is the price's percentile wearing a multiple's
# clothes, and a reader who is not told cannot know.
PERCENTILE_BASIS_LABELS = {
    "price_only": "只反映股价高低（这段历史里基本面没有变过）",
    "price_and_filed_fundamentals": "股价与已报基本面一起算出来的",
}
CHANGE_REASON_LABELS = {
    "filing_actual": "财报实际值替代原估计",
    "driver_event": "新事件改变了关键驱动因素",
    "assumption_review": "关键假设已经复核",
    "evidence_thicker": "支撑论据已补充更新",
    "human_revision": "人工审阅后修订",
    "imported_prior": "从历史资料导入",
}
ASSUMPTION_KIND_LABELS = {
    "estimate": "模型估算", "human": "人工设定", "actual": "已披露实际值",
}
QUALITY_CHECK_LABELS = {
    "numbers_without_refs": "数字均有可核验出处",
    "residual_citation_artefacts": "没有残留引用标记",
    "duplicate_parallel_citations": "没有重复引用同一事项",
    "required_sections_present": "必需章节完整",
    "claim_refs_resolve": "引用的研究结论均可定位",
    "cites_only_shown_claims": "仅引用输入中已有的研究结论",
    "confidence_stated": "已说明结论置信度",
    "every_section_cites": "各章节均有依据",
    "new_version_cites_new_refs": "新版本引用了新增证据",
    "restatement_drift": "改写保持原意",
}
# What each status means, in the owner's language. The driver's own ``reason``
# is English and written for whoever reads a tick summary -- "this mission does
# not grant market_price in autonomy.may_write" is the right sentence in the
# wrong place -- so it moves to a detail line and the owner reads this instead.
LANE_STATUS_NOTES = {
    "launched": "任务已启动",
    "busy": "上一项任务仍在运行",
    "idle": "配置正常，本轮没有待办",
    "held": "上次执行未完成，暂不重复尝试",
    "waiting": "条件满足后继续运行",
    "rejected": "本次结果未被接受",
    "unconfigured": "当前环境尚未配置该流程",
    "ungranted": "当前研究目标尚未授权该流程写入",
    "unavailable": "本轮无法取得所需资料或依赖",
    "unstarted": "当前环境尚无该流程的执行记录",
    "current": "当前结果已是最新版本",
    "failed": "本次执行失败",
    "terminal": "本次任务已结束，需更新资料或条件后再重新运行",
    "recovery_required": "上次执行留下待恢复事项，本轮未继续处理",
    "duplicate": "已有相同结果，无需重复生成",
    "dispatched": "本轮任务已派发处理",
    "proposed": "已提交重审建议，等待你的决定",
    "irrelevant": "当前没有需要处理的事项",
}
# The lanes the registry knows about, named for the owner. A lane with no name
# here still appears -- silence about a lane is exactly what this panel exists
# to end -- under its own key, which is ugly but visible.
REGISTRY_LANE_LABELS = {
    "guidepoint_discovery": "检索第三方专家访谈纪要库",
    "mission_sec_quarters": "提取 SEC 季度财务报表核心数据",
    "mission_statements": "获取完整财务三张表",
    "mission_market_prices": "同步每日收盘行情与成交量",
    "mission_tracking": "持续跟踪已覆盖公司",
    "mission_catalyst_calendar": "跟踪业绩披露、投资者交流日与静默期日程",
    "mission_consensus": "同步卖方一致预期",
    "company_model_spec": "定义公司财务模型科目与驱动框架",
    "mission_model_spec": "定义公司财务模型科目与驱动框架",
    "company_model_forecast": "测算财务预测科目与衍生指标",
    "mission_model_forecast": "测算财务预测科目与衍生指标",
    "forecast_sensitivity": "核心假设敏感性分析与历史区间回测",
    "mission_sensitivity": "核心假设敏感性分析与历史区间回测",
    "claim_index": "构建研究论点与证据索引库",
    "mission_claim_index": "构建研究论点与证据索引库",
    "research_plan": "制定下一步研究计划",
    "initial_screen": "起草初步研究筛查报告",
    "event_judgement": "评估最新市场动态对投资观点的影响",
    "mission_event_judgement": "评估最新市场动态对投资观点的影响",
    "earnings_season": "财报前瞻与业绩对标复盘",
    "debate_map": "梳理市场核心分歧与差异化观点",
    "mission_debate_map": "梳理市场核心分歧与差异化观点",
    "debate_map_verifier": "独立核验市场分歧分析",
    "mission_crowd_sources": "监测散户舆情与职场评价",
    "mission_stage": "记录并推进研究阶段",
    "claim_review": "复核既有研究结论",
    "sales_notes_feed": "精读卖方销售快报与晨会纪要",
    "company_wiki_feed": "提取公司知识库与深度访谈纪要",
    "prior_research": "检索内部历史投研资料",
    "research_task": "开展专项研究",
    "mission_annual_research": "基于已获取年报开展专题研究",
    "mission_document_research": "基于已获取原文开展专题研究",
    "mission_reflection": "复盘每周投研资源分配",
    "company_dossier": "编纂公司深度投研档案",
    "deep_insight_gate": "完成深度认知十二问并提交人工决策",
    "mission_ownership": "追踪机构持仓与高管交易披露",
    "mission_hkex_filings": "监测港股回购与董事增减持披露",
    "conviction_call": "形成核心投资建议并提交人工决策",
    "mission_conviction": "形成核心投资建议并提交人工决策",
    "conviction_call_verifier": "独立核验投资判断",
    "mission_reopen": "评估已通过阶段评审的报告是否需要更新",
    "catalog_sync": "同步可用模型目录",
    "industry_framework": "构建行业分析框架与多标的横向对比",
    "model_stage_bridge": "校验行业与公司模型勾稽并推进研究阶段",
    "investment_memo": "起草投资备忘录，经独立复核后提交人工决策",
    "zero_base_review": "月度归零复核：降低锚定偏差并重估投资假设",
}
# Already shown by name above the registry rows, with their budgets.
LANES_SHOWN_ELSEWHERE = frozenset({"mission_source_discovery", "document_extraction"})


# P17d 四格: the six words the lane panel counts by, in the owner's language.
# The panel is a count of the rows already on the page, not a second reading of
# the heartbeat: the number in the tile and the rows below it cannot disagree.
LANE_STATUS_BUCKETS: dict[str, str] = {
    "ungranted": "缺少任务授权", "unconfigured": "尚未配置", "unapproved": "等待审批",
    "idle": "本轮无待办", "running": "正在运行", "held": "暂停重试",
}
# Which lane status word falls in which bucket.  A word this table has never
# seen is counted under ``other`` rather than silently dropped -- an
# uncountable lane is the thing the panel exists to end.
LANE_STATUS_BUCKET_OF: dict[str, str] = {
    "ungranted": "ungranted",
    "unconfigured": "unconfigured", "unstarted": "unconfigured",
    "unapproved": "unapproved",
    "proposed": "unapproved",
    "idle": "idle", "current": "idle",
    "launched": "running", "busy": "running",
    "held": "held", "failed": "held", "unavailable": "held",
    "rejected": "held", "terminal": "held", "recovery_required": "held",
    "duplicate": "idle",
}

# P17d: what a parked work item is waiting on, in the owner's language.  The
# ops backlog is the one page whose whole job is to be actionable, so a
# dependency shown as ``alphaengine_desktop`` would be a page that needs a
# programmer to read it.
DEPENDENCY_LABELS: dict[str, str] = {
    "alphaengine_desktop": "AlphaEngine 桌面端会话（要重新登录）",
    "alphaengine": "AlphaEngine",
    "guidepoint": "Guidepoint",
    "sec": "SEC EDGAR",
    "market_data": "行情源（yfinance）",
    "openclaw": "OpenClaw 网关",
    "model": "模型服务",
    "model_budget": "模型调用预算",
    "quota": "数据源配额",
    "transport": "网络 / 连接器",
    "writer_rpc": "写入服务（重启中或忙）",
    "source": "外部数据源",
    "unknown": "未识别的依赖（原始记录见技术详情）",
}
FAILURE_CLASS_LABELS: dict[str, str] = {
    "dependency_unavailable": "依赖不可用：等它回来，不算重试次数",
    "content_refused": "产出未通过内容或证据校验：不原样重试",
    "not_permitted": "待授权：权限或治理配置改变后再继续",
    "transient": "临时失败：有限次重试",
}


def _terminal_display_reason(reason: Any, failure_class: Any = None) -> str:
    """Translate a terminal ledger reason without changing its audit record.

    These matches describe closed validator outcomes already emitted by the
    product.  They are display-only: an unknown sentence gets the generic
    explanation and never acquires a new failure classification.
    """

    text = str(reason or "").casefold()
    category = str(failure_class or "")
    if "budget_refused" in text or "budget" in text and "refus" in text:
        if any(marker in text for marker in ("day_cap", "daily", "pool_exhausted")):
            return "当天适用的预算池或日额度已用尽"
        return "本次请求超出适用的调用或任务预算限制"
    if category and category != "content_refused":
        return {
            "dependency_unavailable": "依赖当前不可用",
            "not_permitted": "当前任务或治理规则尚未授权",
            "transient": "临时失败已达到本次重试上限",
        }.get(category, "任务已结束，具体原因见技术详情")
    if "numbers_without_refs" in text or "number_not_in_source" in text:
        return "数字缺少可核验来源"
    if any(marker in text for marker in (
        "longer than", "must be at most", "must be exactly", "got keys",
    )):
        return "输出格式或长度不符合要求"
    if any(marker in text for marker in (
        "cites nothing", "no reference", "not in any of the cited sources",
    )):
        return "现有证据不支持这份产出"
    if text.strip() == "verification_failed":
        return "产出未通过独立核验"
    if "segment_sum:" in text or "rate_domain:" in text:
        return "历史数字未通过勾稽或单位校验"
    if "empty_body" in text or "empty response body" in text:
        return "来源页面没有返回正文，因此未登记为可用资料"
    if "public web fetch returned http" in text:
        return "来源页面拒绝访问或返回了失败状态"
    return "当前产出未通过内容或证据校验"


def _ops_waiting_reason(reason: Any, *, permission: bool = False) -> str:
    """Describe a parked ledger row without exposing its machine exception."""

    text = str(reason or "").casefold()
    if "http error 403" in text or "http 403" in text:
        return "数据来源拒绝访问（HTTP 403），已暂缓获取"
    if "pool_exhausted" in text:
        if "event_response" in text:
            return "事件响应模型预算池今天的余额不足"
        if "coverage" in text:
            return "公司持续研究模型预算池今天的余额不足"
        if "adhoc" in text:
            return "专项研究模型预算池今天的余额不足"
        if "maintenance" in text:
            return "研究维护模型预算池今天的余额不足"
        return "本次工作使用的模型预算池今天余额不足"
    if "budget_refused" in text or ("budget" in text and "refus" in text):
        if any(marker in text for marker in ("day_cap", "daily", "mission_budget_exceeded")):
            return "本研究任务今天的模型预算余额不足"
        return "本次请求超出适用的调用或任务预算限制"
    if "model_chain_exhausted" in text:
        return "可用模型链均未成功完成本次请求"
    if permission or "governance" in text and "not approved" in text:
        if "yfinance-calendar" in text:
            return "财报与分红日程的数据源尚未获得使用批准"
        if "analyst-estimates" in text:
            return "卖方一致预期数据源尚未获得使用批准"
        if "daily-prices" in text:
            return "每日行情数据源尚未获得使用批准"
        return "相关数据源或操作尚未获得使用批准"
    if "alphaengine" in text:
        return "AlphaEngine 桌面端当前不可用"
    return "任务正在等待依赖恢复，具体原因见技术详情"


def _ops_item_label(item_key: Any,
                    members: Mapping[str, Mapping[str, Any]]) -> str:
    """Return the human company label bound at the start of a work item key."""

    raw = str(item_key or "")
    company_ref = raw.split("|", 1)[0]
    if company_ref.startswith("company:"):
        return CockpitPlane._label(members, company_ref)
    if company_ref.startswith("industry:"):
        return "行业任务"
    return "任务记录"

def _ops_superseded_mission(item_key: Any, current: str | None) -> bool:
    """Recognise an exact older same-scope mission binding in any key segment."""
    if current is None or not isinstance(item_key, str):
        return False
    marker = "coverage-mission-version:"
    candidates = [part for part in item_key.split("|") if part.startswith(marker)]
    if not candidates:
        return False
    current_scope, separator, current_number = current.rpartition(":")
    if not separator or not current_number.isdigit():
        return False
    parsed = []
    for candidate in candidates:
        scope, separator, number = candidate.rpartition(":")
        if not separator or not number.isdigit():
            return False
        parsed.append((scope, int(number)))
    return all(scope == current_scope and version < int(current_number)
               for scope, version in parsed)


def _ops_model_spec_history_reason(
        item: Mapping[str, Any], latest: Mapping[str, Mapping[str, Any]],
        latest_inputs: Mapping[str, Mapping[str, Any]] | None = None) -> str | None:
    """Classify a failed spec only from a later success or newer input."""
    if item.get("lane") != "mission_model_spec":
        return None
    item_key = item.get("item_key")
    if not isinstance(item_key, str):
        return None
    parts = item_key.split("|")
    if (len(parts) < 2 or not parts[0].startswith("company:")
            or re.fullmatch(r"[0-9a-f]{64}", parts[1]) is None):
        return None
    successful = latest.get(parts[0])
    last_seen = item.get("last_seen")
    later_success = (isinstance(successful, Mapping)
                     and isinstance(successful.get("state_hash"), str)
                     and re.fullmatch(r"[0-9a-f]{64}", successful["state_hash"]) is not None
                     and isinstance(successful.get("created_at"), str)
                     and isinstance(last_seen, str)
                     and successful["created_at"] > last_seen)
    current_input = (latest_inputs or {}).get(parts[0])
    later_input = (isinstance(current_input, Mapping)
                   and isinstance(current_input.get("state_hash"), str)
                   and re.fullmatch(r"[0-9a-f]{64}", current_input["state_hash"]) is not None
                   and current_input["state_hash"] != parts[1]
                   and isinstance(current_input.get("last_seen"), str)
                   and isinstance(last_seen, str)
                   and current_input["last_seen"] > last_seen)
    if later_success:
        return "later_success"
    if later_input:
        return "newer_input"
    return None


def _ops_superseded_model_spec(
        item: Mapping[str, Any], latest: Mapping[str, Mapping[str, Any]],
        latest_inputs: Mapping[str, Mapping[str, Any]] | None = None) -> bool:
    return _ops_model_spec_history_reason(item, latest, latest_inputs) is not None


def _runtime_error_display(reason: Any) -> str:
    """Classify a runtime error conservatively for the activity page."""
    text = str(reason or "").casefold()
    if "http error 403" in text or "http 403" in text:
        return "数据来源拒绝访问（HTTP 403）"
    if "budget" in text or "allowance" in text:
        return "任务受到当前费用或用量限制"
    if any(word in text for word in ("permission", "forbidden", "not permitted", "unauthorized")):
        return "当前任务或治理规则尚未授权"
    if any(word in text for word in ("timeout", "timed out", "busy", "locked")):
        return "系统繁忙或等待超时，可以稍后重试"
    if any(word in text for word in ("unavailable", "connection", "network")):
        return "所需服务或资料暂时不可用"
    return "本次运行未完成，具体原因见技术详情"


OUTCOME_LABELS = {
    "should_have_moved": "当时应调整但未调整（候选）",
    "held": "维持原判断正确",
    "moved_right": "调整方向正确",
    "moved_wrong": "调整方向错误",
    "not_confirmed": "判断方向尚未得到证实",
    "pending": "尚待评估",
    "unavailable": "暂不具备评估条件",
}


def _judgement_outcome_panel(metric: Any) -> dict[str, Any]:
    """W4: the judgement outcome counts, in the owner's words.

    Answers ``available: false`` with the reason on a Core whose zero-base
    lane has never run, which is what every other reader on this page does and
    is the only honest thing to show: a row of zeroes would say every decision
    we ever made was right.
    """

    if not isinstance(metric, Mapping):
        return {"available": False, "reason": "这一版回头看还没有判断结果台账"}
    if not metric.get("available"):
        return {"available": False, "reason": metric.get("reason")}
    to_date = metric.get("to_date") or {}
    return {
        "available": True,
        "checked": metric.get("checked"),
        "companies": metric.get("companies"),
        "rows": [
            {"outcome": outcome, "label": OUTCOME_LABELS.get(outcome, outcome),
             "to_date": int(to_date.get(outcome) or 0),
             "this_week": int((metric.get("this_week") or {}).get(outcome) or 0)}
            for outcome in OUTCOME_LABELS
            if (to_date.get(outcome) or 0) or
            ((metric.get("this_week") or {}).get(outcome) or 0)
        ],
        "should_have_moved": metric.get("should_have_moved"),
        "moved_right": metric.get("moved_right"),
        "note": "这些是候选，不是绩效考核：公式冻结、可重放，没有模型参与。",
    }

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
    "sales_note": "卖方销售简报", "crowd_post": "散户与市场讨论",
    "expert_excerpt": "专家访谈摘录",
    "insider_transaction": "董事与高管的买卖",
    "insider_trading_plan": "董事与高管的交易计划变更",
    "ownership_change": "大股东持股变化",
    "holdings_change": "机构持仓变化",
    "buyback_disclosure": "公司回购自己的股票",
}
# Ordered best first, the same order the Playbook reads them in.
EVIDENCE_TIER_LABELS = {
    "primary_filing": "公司报表原文", "management_direct": "管理层直接表述",
    "expert_network": "专家访谈", "sell_side": "卖方观点",
    "vendor_note": "第三方资料整理", "internal_wiki": "内部研究档案",
    "market_price": "市场价格", "derived": "系统计算结果",
    "news_media": "新闻报道", "crowd": "公开市场讨论",
}
# The five words the judgement layer may say, and the six things it may do.
JUDGEMENT_DECISION_LABELS = {
    "NO_CHANGE": "维持现有判断", "THESIS_STRENGTHENED": "论点得到加强",
    "THESIS_WEAKENED": "论点被削弱了", "THESIS_BROKEN": "论点被打破了",
    "NEW_THESIS": "形成新的投资论点",
}
JUDGEMENT_ACTION_LABELS = {
    "no_change": "维持现状", "note": "记录研究说明",
    "research": "启动专项研究", "revise_forecast": "修订预测",
    "revise_thesis": "提出论点修订候选", "revise_dossier": "修订公司档案",
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
    "expert_excerpt": "专家访谈摘录", "sales_note": "卖方销售简报",
    "crowd_post": "散户帖子", "employee_review": "员工评价",
    "news": "新闻", "filing": "公司报表", "financial_statement": "三张报表",
    "price": "股价", "consensus": "市场一致预期", "calendar": "日程",
    "web_page": "公开网页", "ownership_filing": "股东与高管持股申报",
    "insider_trading_plan": "高管预设交易计划", "buyback_disclosure": "股份回购披露",
}
SOURCE_OPERATION_LABELS = {
    "get_document": "读取文档", "search_library": "搜索资料库",
    "ah_premium": "A／H 股溢价", "buybacks": "股份回购",
    "financial_statements": "财务报表", "margin_balance": "融资融券余额",
    "northbound_flow": "北向资金流", "shareholders": "股东资料",
    "list_documents": "列出文档", "blind_reviews": "Blind 员工评价",
    "announcements_index": "公告索引", "disclosure_of_interests": "权益披露",
    "monthly_returns": "月报表", "next_day_disclosure_returns": "翌日披露报表",
    "get_note": "读取销售简报", "list_notes": "列出销售简报",
    "list_filings": "列出公司申报", "beneficial_ownership": "实益所有权申报",
    "form13f_holdings": "13F 机构持仓", "form144_notices": "Form 144 拟出售通知",
    "form4_transactions": "Form 4 高管交易", "analyst_estimates": "分析师一致预期",
    "calendar": "公司日程", "daily_prices": "每日股价",
    "search_web": "搜索公开网页", "fetch_get": "读取公开网页",
}
CONFIDENCE_LABELS = {"high": "高", "medium": "中等", "low": "低"}

REVIEWED_CLAIM_PERIOD_LABELS = {'1990 to April 2020 onward': '1990年至2020年4月，此后持续',
 '1Q and 2Q (as of 2026 mid-year survey)': '第一季度和第二季度（截至2026年年中调查）',
 '1Q27E-4Q27E': '2027年第一季度预测值至第四季度预测值',
 '2010s': '2010年代',
 '2016-2022 tenure at Infosys': '2016年至2022年在Infosys任职期间',
 '2021 onward': '自2021年起',
 '2023 and forward': '2023年及以后',
 '2023-2024, near term': '2023年至2024年及近期',
 '2024 and forecast period': '2024年及预测期',
 '2024/2025 market report': '2024/2025年市场报告',
 '2025 and beyond': '2025年及以后',
 '2025 and forward': '2025年及以后',
 '2025 and through 2034': '2025年至2034年',
 '2025 assessment': '2025年评估期',
 '2025 onward': '自2025年起',
 '2025 onwards': '2025年及以后',
 '2025 program outlook': '2025年项目展望',
 '2025 program, through second quarter 2026': '2025年项目，至2026年第二季度',
 '2025 results and 2026 outlook': '2025年业绩及2026年展望',
 '2025 year-end': '2025年年末',
 '2026 (article date April 2026)': '2026年（文章日期为2026年4月）',
 '2026 (six months ended June 30, 2026)': '2026年（截至2026年6月30日的六个月）',
 '2026 Citi AI Summit': '2026年花旗AI峰会期间',
 '2026 H2': '2026年下半年',
 '2026 Investor Day outlook': '2026年投资者日展望',
 '2026 Investor Day, through FY29': '2026年投资者日及截至2029财年的展望',
 '2026 Investor and Analyst Day': '2026年投资者与分析师日',
 '2026 Q1 earnings call': '2026年第一季度业绩电话会',
 '2026 Think conference (May 2026)': '2026年Think大会（2026年5月）',
 '2026 YTD': '2026年年初至报告期',
 '2026 and beyond': '2026年及以后',
 '2026 into early 2027': '2026年至2027年初',
 '2026 mid-year CIO survey': '2026年年中CIO调查',
 '2026 mid-year vs. January 2026': '2026年年中与2026年1月对比',
 '2026 pivotal year; 36-month target timeframe': '2026年这一关键年份；目标期为36个月',
 '2026 versus 2025 (six months ended June 30)': '2026年与2025年比较（截至6月30日的六个月）',
 '2026, next few years': '2026年及未来几年',
 '2026年Q2': '2026年第二季度',
 '2026年Q2及展望': '2026年第二季度及展望',
 '2030 time frame': '以2030年为时间范围',
 '2H and fiscal 2026': '下半年及2026财年',
 '2H2026 to early 2027': '2026年下半年至2027年初',
 '2Q and second half, exit rate into next year': '第二季度及下半年，以及进入下一年度时的期末水平',
 '2Q results update': '第二季度业绩更新期',
 '3-6 years (unspecified horizon)': '3至6年（具体起止时间未说明）',
 '3Q peak': '第三季度峰值',
 'AI era': 'AI时代',
 'AI native era': 'AI原生时代',
 'Accenture fiscal Q3 2025': 'Accenture 2025财年第三季度',
 'Annual': '年度',
 'Annual Report on Form 10-K': '10-K表格年度报告',
 'As of the CEO profile (no explicit dated period)': '截至CEO简介所述时点（未注明具体日期）',
 'At CEO appointment (January 2023)': 'CEO于2023年1月上任时',
 'At CEO transition (January 2023)': 'CEO交接时（2023年1月）',
 'At announcement (2019)': '公告时点（2019年）',
 'Business Leaders 2.0 List (no explicit date)': 'Business Leaders 2.0榜单（未注明明确日期）',
 'By Q4 2026': '截至2026年第四季度',
 'CEO transition announcement (late 2022)': 'CEO交接公告（2022年末）',
 'CEO transition interview (forward-looking)': 'CEO交接访谈（前瞻性）',
 'CEO transition period, upcoming 12 months': 'CEO交接期及未来12个月',
 'CGI tenure period': 'CGI任职期间',
 'CY2025E exposure context; F3Q26 analysis': '2025自然年预测值敞口背景；2026财年第三季度分析期',
 'CY26 downside scenario': '2026自然年下行情景',
 'CY26 upside scenario': '2026自然年乐观情景',
 'Career prior to and including Cognizant tenure (no explicit date)': '加入Cognizant之前及在Cognizant任职期间（未注明明确日期）',
 'Coming quarters and longer term': '未来几个季度及更长期',
 'Coming weeks and months': '未来几周和几个月',
 'Company description (fiscal year ending March 31, 2024 context)': '公司说明（以截至2024年3月31日的财年为背景）',
 'Company profile (2026)': '公司概况（2026年）',
 'Company profile (as of filing window)': '截至申报窗口的公司简介',
 'Current fiscal year to 2030': '当前财年至2030年',
 'December 2023 onward': '自2023年12月起',
 'Earlier in 2025': '2025年早些时候',
 'Effective March 31, 2026': '自2026年3月31日起生效',
 'Effective September 1, 2025': '自2025年9月1日起生效',
 'Effective September 2024': '2024年9月起生效',
 'F1Q (June quarter)': '第一财季（截至6月的季度）',
 'F27': '2027财年',
 'F3Q26 (quarter ended March 2026)': '2026财年第三季度（截至2026年3月的季度）',
 'F3Q26 (quarter ending March 2026)': '2026财年第三季度（截至2026年3月的季度）',
 'F3Q26 onward': '2026财年第三季度及以后',
 'FY2024 year-to-date through FQ3': '2024财年年初至第三季度',
 'FY2024A-FY2027E': '2024财年实际值至2027财年预测值',
 'FY2025 exit from Q4': '2025财年第四季度末水平',
 'FY2025 onward': '2025财年及以后',
 'FY2025 through Q3 FY2026': '2025财年至2026财年第三季度',
 'FY2025 through early 2026': '2025财年至2026年初',
 'FY2026 Investor Day (Jun 2026)': '2026财年投资者日（2026年6月）',
 'FY2026 YTD': '2026财年年初至报告期',
 'FY2026 and beyond': '2026财年及以后',
 'FY2026 and prior': '2026财年及此前期间',
 'FY2026 onwards': '2026财年及以后',
 'FY24 year-to-date': '2024财年年初至报告期',
 'FY24A-FY28E': '2024财年实际值至2028财年预测值',
 'FY25A-FY28E': '2025财年实际值至2028财年预测值',
 'FY25A-FY29E': '2025财年实际值至2029财年预测值',
 'FY26 and beyond': '2026财年及以后',
 'FY26 through year-end': '2026财年至该财年年末',
 'FY26-27E': '2026至2027财年预测',
 'FY26E to FY27E': '2026财年预测值至2027财年预测值',
 'FY27 estimates': '2027财年预测值',
 'FY27 guidance and FY29 medium-term target': '2027财年指引及2029财年中期目标',
 'FY27 through FY29': '2027财年至2029财年',
 'First 12 months as CEO (forward-looking)': '担任CEO后的首12个月（前瞻性）',
 'First half of 2026': '2026年上半年',
 'First half of fiscal 2027': '2027财年上半年',
 'First nine months (most recent fiscal year)': '最近一个财年的前九个月',
 'Fiscal 2023 onward': '自2023财年起',
 'Fiscal 2025 Q4 and after': '2025财年第四季度及以后',
 'Fiscal 2025 exit from Q4': '2025财年第四季度末水平',
 'Fiscal 2025 full year': '2025财年全年',
 'Fiscal 2Q reported quarter': '已报告的财年第二季度',
 'Fiscal 2Q reported quarter and second-half outlook': '已报告的第二财季及下半年展望',
 'Fiscal year (reported full year)': '财年（已披露全年数据）',
 'Fiscal year-to-date Q3 FY26': '2026财年年初至第三季度',
 'Forecast period': '预测期',
 'Forecast period 2025-2035': '2025至2035年预测期',
 'Forecast period 2026–2035': '2026年至2035年预测期',
 'Forward-looking strategy commentary': '前瞻性战略说明所涉及的期间',
 'Fourth quarter of the prior year': '上一年第四季度',
 'Full year': '全年',
 'Full year 2025': '2025全年',
 'Full year 2026': '2026年全年',
 'Full-year 2022 guidance update': '2022年全年指引更新',
 'Future': '未来期间',
 'Future periods': '未来期间',
 'H1 2026': '2026年上半年',
 'H1 2026 vs H1 2025': '2026年上半年与2025年上半年对比',
 'H2 2025 into 2026': '2025年下半年至2026年',
 'H2 2026': '2026年下半年',
 'H2 FY2026': '2026财年下半年',
 'H2 FY2027': '2027财年下半年',
 'January 2020 announcement': '2020年1月公告',
 'July 2025 earnings call onwards': '自2025年7月业绩电话会起',
 'June 2025 announcement': '2025年6月公告',
 'June 2025 forward-looking statements': '2025年6月的前瞻性表述',
 'June of the preliminary quarter, 2026': '2026年初步披露季度的6月',
 'Krishna tenure': 'Krishna任期',
 'Last twelve months': '过去十二个月',
 'Late 2019 to Q2 2020': '2019年末至2020年第二季度',
 'Mar 2026 conference': '2026年3月会议',
 'March 2026 to present': '2026年3月至今',
 'May 2026 conference preview': '2026年5月会议前瞻',
 'Mid-Year 2026': '2026年年中',
 'Most recent quarter': '最近一个季度',
 'Near term': '近期',
 'Near term and AI era outlook': '近期及AI时代展望',
 'Near-term': '近期',
 'Next 3 years': '未来3年',
 'Next 5-10 years': '未来5至10年',
 'Next five to six years': '未来五至六年',
 'Next ~18 months': '未来约18个月',
 'Not stated': '未说明',
 'PT end date Dec-27': '目标价截止日：2027年12月',
 'Past 10-15 years and next 10-15 years': '过去10至15年及未来10至15年',
 'Post-2026 through 2028': '2026年后至2028年',
 'Pre-2023': '2023年以前',
 'Prior to 2022': '2022年以前',
 'Q1 2026 and H2 2026': '2026年第一季度及下半年',
 'Q1 2026 and forward quarters': '2026年第一季度及此后各季度',
 'Q1 2026 and full year 2026': '2026年第一季度及2026全年',
 'Q1 2026 announcement, multiyear': '2026年第一季度公告，涉及多年期间',
 'Q1 2026 update since AI Day': '自AI Day以来的2026年第一季度更新',
 'Q1 FY25 (three months ended June 30, 2024)': '2025财年第一季度（截至2024年6月30日的三个月）',
 'Q1 and full year current': '本年度第一季度及全年',
 'Q1 and next 1-2 quarters': '第一季度及随后一至两个季度',
 'Q1 and trailing twelve months of the reported fiscal year': '所报告财年的第一季度及过去十二个月',
 'Q1 of the reported fiscal year': '已披露财年的第一季度',
 'Q1 to Q3 fiscal 2026': '2026财年第一至第三季度',
 'Q2 (quarter reported)': '第二季度（已报告季度）',
 'Q2 2024 and H2 2024': '2024年第二季度及下半年',
 'Q2 2026 (April-May)': '2026年第二季度（4月至5月）',
 'Q2 2026 (announcement period)': '2026年第二季度（公告期）',
 'Q2 2026 (early)': '2026年第二季度初期',
 'Q2 2026 (sixth consecutive quarter)': '2026年第二季度（连续第六个季度）',
 'Q2 2026 / H2 2026': '2026年第二季度及2026年下半年',
 'Q2 2026 and forward': '2026年第二季度及以后',
 'Q2 2026 and full year 2026': '2026年第二季度及2026年全年',
 'Q2 2026 and remainder of 2026': '2026年第二季度及2026年剩余期间',
 'Q2 2026 and second half': '2026年第二季度及下半年',
 'Q2 2026 earnings call': '2026年第二季度业绩电话会',
 'Q2 2026; revenue contribution expected H1 2027': '2026年第二季度；预计收入贡献期为2027年上半年',
 'Q2 FY2026 (quarter ending February 2026)': '2026财年第二季度（截至2026年2月的季度）',
 'Q2 and comparison to prior quarter': '第二季度与上一季度对比',
 'Q2 and first half of year': '第二季度及上半年',
 'Q2 and full year 2026': '2026年第二季度及全年',
 'Q2 and remainder of 2026': '第二季度及2026年剩余期间',
 'Q2 and remainder of the year': '第二季度及当年剩余期间',
 'Q2 fiscal 2026 onward': '自2026财年第二季度起',
 'Q3 2026 onwards': '2026年第三季度及以后',
 'Q3 FY2024 (quarter ended May 31, 2024)': '2024财年第三季度（截至2024年5月31日的季度）',
 'Q3 fiscal 2024 year-to-date': '2024财年年初至第三季度',
 'Q4 2025 / trailing three years': '2025年第四季度／过去三年',
 'Q4 and full year 2025': '2025年第四季度及全年',
 'Q4 and going forward': '第四季度及以后',
 'Q4 of the current fiscal year': '当前财年第四季度',
 'Recent periods including FY2026': '包括2026财年在内的近期期间',
 'Remainder of 2026': '2026年剩余期间',
 'Remainder of fiscal year': '财年剩余期间',
 'Report period (2025–2035)': '报告期（2025年至2035年）',
 'Reported quarter': '已披露季度',
 'Rolling four quarters': '滚动四个季度',
 'Rometty tenure': 'Rometty任职期间',
 'Rometty tenure (2012-2020)': 'Rometty任期（2012至2020年）',
 'Second half of 2026': '2026年下半年',
 'Second half of fiscal 2026': '2026财年下半年',
 'Second half of fiscal year': '财年下半年',
 'Second half of the year': '当年下半年',
 'September 2023 announcement, effective December': '2023年9月公告，12月生效',
 'September 2025 onward': '自2025年9月起',
 'Since 1993 (company description)': '自1993年以来（公司简介）',
 'Since C4Q24': '自2024自然年第四季度起',
 'Starting July, Q2 2026': '自7月起；2026年第二季度',
 'Think 2026 event': 'Think 2026活动',
 'Third quarter and near future': '第三季度及近期展望',
 'Three consecutive years': '连续三年',
 'Through 2030': '至2030年',
 'Through 2034': '截至2034年',
 'Through fiscal 2029': '截至2029财年',
 'Trailing 12 months': '过去12个月',
 'Undated (CEO bio profile)': '未注明日期（CEO履历）',
 'Upcoming period': '未来期间',
 'YE27': '2027年末',
 'about two years ago to present': '约两年前至今',
 'acquisition effective June 22, 2026': '收购于2026年6月22日生效',
 'acquisition years through 2025': '截至2025年的收购年度',
 'age of artificial intelligence (current/future)': '人工智能时代（当前及未来）',
 'announced June 2024, effective September 2024': '2024年6月宣布，2024年9月生效',
 'announced June 2024; effective December 2024': '2024年6月宣布，2024年12月生效',
 'announced June 2024; three-year horizon': '2024年6月宣布；期限为三年',
 'announced June 2025': '2025年6月宣布',
 'announced June 2025, effective September 1, 2025': '2025年6月宣布，2025年9月1日生效',
 'announced Oct 2025': '2025年10月宣布',
 'annual': '年度',
 'annual 2026': '2026年度',
 'any particular period': '任一特定期间',
 'around 08-07': '约在 08-07 前后（日期格式未注明）',
 'around 08-26': '约在 08-26 前后（日期格式未注明）',
 'around one-year anniversary of partnership': '合作约满一周年时',
 'as early as 2029': '最早于2029年',
 'as of August 2026, through the year': '截至2026年8月，并延续至年末',
 'as of Jul 31': '截至7月31日',
 'as of late 2023': '截至2023年底',
 'as of late 2024': '截至2024年末',
 'as of the 2026 announcement period': '截至2026年公告期',
 'as of the June 20, 2025 announcement coverage': '截至2025年6月20日公告相关报道',
 'as of the referenced earnings call': '截至所引用的业绩电话会',
 'as of the source date (August 2026)': '截至来源日期（2026年8月）',
 'as reported (no specific period)': '按已披露口径（未注明具体期间）',
 'at CEO departure announcement': 'CEO离任公告时点',
 'at CEO transition (Jan 2023)': 'CEO交接时（2023年1月）',
 'at appointment': '任命时',
 'at interim CEO appointment': '临时CEO任命时点',
 'at resignation': '辞职时',
 'at the time of the announcement': '公告时',
 'at the time of the executive change': '管理层变动时点',
 'at the time of the report': '报告发布时',
 'back end of fiscal year 2024': '2024财年后半段',
 'before June 2024': '2024年6月以前',
 'beginning 2025, over ~3 year bookings duration': '自2025年开始，订单覆盖期约3年',
 'beginning in 2025, as of mid-2026': '自2025年起，截至2026年年中',
 'beginning in 2025; as of August 2026': '始于2025年；截至2026年8月',
 'beyond 2027 through 2030': '2027年以后至2030年',
 'by 2026': '截至2026年',
 'by 2029': '截至2029年',
 'by end of 2024': '截至2024年末',
 'calendar 2026 revised outlook': '2026自然年修订展望',
 'calendar year': '自然年',
 'calendar year 2024': '2024自然年',
 'calendar year to date': '本自然年年初至报告期',
 'closed July 2019': '2019年7月完成',
 'coming months; first half of calendar 2026': '未来数月；2026自然年上半年',
 'coming quarters': '未来几个季度',
 'coming years': '未来几年',
 'company description updated 25th July 2024': '公司说明更新于2024年7月25日',
 'conversion expected early 2027': '预计于2027年初完成转换',
 'current (FY2024 exit)': '当前（2024财年末水平）',
 'current (GenAI era)': '当前（生成式AI时代）',
 'current (as of document date)': '当前（截至文件日期）',
 'current adoption phase': '当前采用阶段',
 'current and future periods': '当前及未来期间',
 'current and going forward': '当前及以后',
 'current and longer term': '当前及较长期',
 'current calendar year': '当前自然年',
 'current fiscal year (nine months)': '本财年（九个月）',
 'current fiscal year and next': '当前财年及下一财年',
 'current fiscal year commentary': '当前财年评论期',
 'current fiscal year, second half': '本财年下半年',
 'current index membership': '当前指数成员身份',
 'current initiative': '当前举措实施期',
 'current initiative, forward-looking': '当前举措及未来展望',
 'current period described in fiscal year 2025': '2025财年所述当前期间',
 'current period versus prior IT outsourcing': '当前期间与此前IT外包时期对比',
 'current quarter (Q4 guidance period)': '当前季度（第四季度指引期）',
 'current quarter and fiscal year-end': '当前季度及财年末',
 'current quarter and full year': '当前季度及全年',
 'current quarter and rest of year': '当前季度及本年剩余期间',
 'current quarter earnings call': '当前季度业绩电话会',
 'current research findings': '当前研究结论对应期间',
 'current strategy (as of document date)': '当前战略（截至文件日期）',
 'current structure': '当前结构',
 'current transformation period': '当前转型期',
 'current, with longer-term outlook': '当前及更长期展望',
 'current; historic track record': '当前及历史记录',
 'current; prior experience': '当前期间；此前经历',
 "during Dobkin's tenure, referenced 2025": 'Dobkin任期内，提及2025年',
 'each reporting period': '每个报告期',
 'earlier this year onward': '自今年早些时候起',
 'early indications, current fiscal year': '本财年初步迹象',
 'earnings call, second quarter ended Feb 28': '业绩电话会：截至2月28日的第二季度',
 'effective March 31, 2026': '自2026年3月31日起生效',
 'effective September 1, 2025': '自2025年9月1日起生效',
 'eight-year tenure ending 2020': '截至2020年的八年任期',
 'established history': '既有历史期间',
 'first 12 months as CEO': '就任CEO后的前12个月',
 'first half 2027 vs second half 2026': '2027年上半年与2026年下半年对比',
 'first half and remainder of the year': '上半年及当年剩余期间',
 'first half and second half': '上半年及下半年',
 'first half and second half of current year': '当前年度上半年及下半年',
 'first half and second half of the year': '上半年及下半年',
 'first half of 2026': '2026年上半年',
 'first half of 2027': '2027年上半年',
 'first half of 2027 versus second half of 2026': '2027年上半年与2026年下半年对比',
 'first half of the year and second half outlook': '本年上半年及下半年展望',
 'first quarter and full year outlook': '第一季度及全年展望',
 'first quarter of fiscal year 2027': '2027财年第一季度',
 'first quarter of fiscal year 2027 and trailing twelve months': '2027财年第一季度及过去12个月',
 'first quarter versus preceding fourth quarter': '第一季度与此前第四季度对比',
 'first three quarters and fourth quarter of current fiscal year': '当前财年前三季度及第四季度',
 'fiscal 2011 to fiscal 2018': '2011财年至2018财年',
 'fiscal 2016 to fiscal 2025': '2016财年至2025财年',
 'fiscal 2020–present': '2020财年至今',
 'fiscal 2023 through fiscal 2025': '2023财年至2025财年',
 'fiscal 2025 and forward': '2025财年及以后',
 'fiscal 2025 and future': '2025财年及未来期间',
 'fiscal 2025 forward': '2025财年及以后',
 'fiscal 2026 and beyond': '2026财年及以后',
 'fiscal 2026 first half': '2026财年上半年',
 'fiscal 2027 and beyond': '2027财年及以后',
 'fiscal Q1 2027 call': '2027财年第一季度电话会',
 'fiscal Q1 ended Nov 30, 2025': '截至2025年11月30日的第一财季',
 'fiscal Q4 and full fiscal year 2026': '2026财年第四季度及全年',
 'fiscal quarter ended June 30, 2026': '截至2026年6月30日的财季',
 'fiscal third quarter ended May 31': '截至5月31日的第三财季',
 'fiscal year 2025 and beyond': '2025财年及以后',
 'fiscal year second half': '财年下半年',
 'fiscal year to date': '本财年年初至报告期',
 'forecast period (2025-2035)': '预测期（2025至2035年）',
 'forecast period 2025-2029': '2025年至2029年预测期',
 'forecast to 2034': '预测至2034年',
 'forward-looking (2026 Investor Day)': '前瞻期间（2026年投资者日）',
 'forward-looking (multi-year)': '前瞻性表述（多年期）',
 'forward-looking (next couple of years)': '前瞻性表述（未来约两年）',
 'forward-looking (unspecified horizon)': '前瞻期间（具体时间范围未说明）',
 'forward-looking, Q2 2026 disclosure': '前瞻性表述：2026年第二季度披露',
 'forward-looking, discussed on the 2025 year-end call': '前瞻期，讨论于2025年年末电话会',
 'forward-looking, stated at the 2025 transition announcement': '前瞻期间，于2025年交接公告时提出',
 'four to five years from current outlook': '从当前展望起四至五年',
 'fourth quarter and full year': '第四季度及全年',
 'fourth quarter referenced on the call': '电话会提及的第四季度',
 'from 2026 onward': '自2026年起',
 'from December 2023': '自2023年12月起',
 'full fiscal year 2026': '2026财年全年',
 'full year': '全年',
 'full year 2025': '2025全年',
 'full year 2026': '2026年全年',
 'future': '未来期间',
 'given period': '所述期间',
 'historical': '历史期间',
 'historical and recent years': '历史及近年',
 'historical through early 2020': '截至2020年初的历史期间',
 'historical through recent years': '截至近年的历史期间',
 'last 12 months': '过去12个月',
 'last 18 months, next 6 months': '过去18个月及未来6个月',
 'last 2-3 years and forward': '过去2至3年及以后',
 'last 4 years; forward': '过去四年及以后',
 'last 4-5 years': '过去4至5年',
 'last 6 months': '过去6个月',
 'last few years': '过去几年',
 'last months and weeks': '过去数月及数周',
 'last quarter': '上一季度',
 'last twelve months': '过去十二个月',
 'last year through present': '去年至今',
 'last years': '过去几年',
 'late 2021 to end 2025': '2021年末至2025年末',
 'late 2023': '2023年末',
 'late 2Q (FY26)': '2026财年第二季度后期',
 'late prior year, discussed on the 2025 year-end call': '上年末，见2025年年末电话会讨论',
 'latest biography': '最新履历所述期间',
 'latest reported quarter': '最近披露季度',
 'latter part of the year expecting activity': '预计活动出现在当年后期',
 'launched just over six weeks ago': '推出至今略超过六周',
 'long run': '长期',
 'long-term history': '长期历史',
 'longer term': '较长期',
 'longer-term': '较长期',
 'mid-2026': '2026年年中',
 'mid-year 2026': '2026年年中',
 'mid-year 2026 survey': '2026年年中调查',
 'mid-year 2026 survey vs. January 2026 survey': '2026年年中调查与2026年1月调查对比',
 'mid-year 2026 survey, forward-looking to 2026': '2026年年中调查，并展望2026年',
 'most recent quarter': '最近一个季度',
 'most recent quarter and full year': '最近一个季度及全年',
 'multi-year, as of the referenced earnings call': '多年期（截至所引用的业绩电话会）',
 'multiple quarters': '多个季度',
 'multiple years': '多年期间',
 'multiyear starting now': '自当前起的多年期',
 'near term': '近期',
 'near term / longer term': '近期／较长期',
 'near term and long term': '近期及长期',
 'near-term': '近期',
 'near-term to medium-term': '近期至中期',
 'next 12 months': '未来12个月',
 'next 24-28 months': '未来24至28个月',
 'next 3-5 years': '未来三至五年',
 'next couple of years': '未来约两年',
 'next earnings call': '下一次业绩电话会',
 'next era': '下一个发展阶段',
 'next few years': '未来数年',
 'next few years from 2026': '自2026年起的未来几年',
 'next five years': '未来五年',
 'next half-decade (CY29/CY30 milestones cited)': '未来约五年（提及2029自然年和2030自然年里程碑）',
 'next quarter': '下一季度',
 'next three years from 2024': '自2024年起未来三年',
 'next twelve months': '未来十二个月',
 'next twelve months from the 10-K filing': '10-K申报后的未来12个月',
 'next two years': '未来两年',
 'next two years from 2026': '自2026年起的未来两年',
 'next wave of growth': '下一轮增长',
 'next year (FY2025)': '下一年（2025财年）',
 'next year and beyond': '明年及以后',
 'next year from 2025': '2025年的下一年',
 'next year through end of decade': '下一年至本十年末',
 'not stated': '未注明',
 'ongoing (unquantified)': '持续进行（未量化）',
 'over past 2.5 years, review dated Jun 12, 2026': '过去2.5年；审阅日期为2026年6月12日',
 'over the past fifteen months': '过去十五个月',
 'over time': '随着时间推移',
 'over time, as of August 2026': '随着时间推移；截至2026年8月',
 'past 13 quarters; since C4Q24': '过去13个季度；自2024自然年第四季度起',
 'past decade through document date': '过去十年至文件日期',
 'past eight years': '过去八年',
 'past three years and current': '过去三年及当前期间',
 'past to present': '过去至今',
 'past two years': '过去两年',
 'past year': '过去一年',
 'past year and future': '过去一年及未来',
 'period of leadership at ICEG prior to September 2024': '2024年9月以前在ICEG的任职期间',
 'post 2Q26 report': '2026年第二季度报告发布后',
 'post-close': '交易完成后',
 'pre-2020 onward': '2020年以前至此后',
 'present': '当前',
 'present (2026 Investor Day)': '当前（2026年投资者日）',
 'prior to 2020': '2020年以前',
 'prior year through current': '上一年至今',
 'quarter discussed in the call (fiscal 2026 Q2 context)': '电话会讨论的季度（2026财年第二季度背景）',
 'quarter ended May 31': '截至5月31日的季度',
 'quarter over quarter': '环比',
 'quarter preceding July 2026': '2026年7月之前的季度',
 'quarter reported in August 2026': '2026年8月报告的季度',
 'quarters up to 2026': '截至2026年的各季度',
 'recent': '近期',
 'recent contract signings': '近期签署的合同',
 'recent earnings season 2026': '2026年近期业绩期',
 'recent fiscal periods': '近期财务期间',
 'recent quarter': '最近一个季度',
 'recent quarters and going forward': '近期各季度及未来期间',
 'recent quarters as of Q2 FY2024': '截至2024财年第二季度的近期几个季度',
 'recent quarters through Q2': '截至第二季度的近期各季度',
 'recent quarters through current quarter': '近期各季度至当前季度',
 'recent quarters up to 2026': '截至2026年的近期几个季度',
 'recent three years': '最近三年',
 'recent years through filing date': '近期数年至申报日期',
 'recently announced': '近期宣布',
 'remainder of 2026': '2026年剩余期间',
 'remainder of the year (2026)': '2026年剩余期间',
 'reported historical quarters': '已披露的历史季度',
 'reported quarter (Q1 FY2027)': '已报告季度（2027财年第一季度）',
 'reporting period of the filing': '申报文件报告期',
 'rest of current year': '本年剩余期间',
 'rest of current year and next': '当前年度剩余期间及下一年度',
 'rest of current year and next year': '当前年度剩余期间及下一年',
 'rest of the year': '本年剩余期间',
 'rest of year and next year': '本年度剩余期间及下一年度',
 'role taken October 2025; source dated August 2026': '2025年10月就任；来源日期为2026年8月',
 'rolling four quarters': '滚动四个季度',
 'second half': '下半年',
 'second half 2026 and LTM': '2026年下半年及过去十二个月',
 'second half and next year': '下半年及下一年',
 'second half of 2026': '2026年下半年',
 'second half of the reported fiscal year': '所报告财年的下半年',
 'second half of the year': '本年下半年',
 'second quarter 2026 and near term': '2026年第二季度及近期',
 'second quarter ended Feb 28': '截至2月28日的第二季度',
 'since 2022': '自2022年起',
 'since February 2022 through fiscal 2025 outlook': '自2022年2月至2025财年展望期',
 'since Investor Day': '自投资者日起',
 'since Investor Day (June 2025)': '自投资者日（2025年6月）起',
 'since September 2018': '自2018年9月以来',
 'six months ended June 30, 2026': '截至2026年6月30日的六个月',
 'the period following the realignment': '调整后的期间',
 'the period of the realignment': '业务调整期间',
 'third and fourth quarters': '第三季度及第四季度',
 'this half year': '本半年',
 'this year': '今年',
 'three consecutive years including current': '包括当前年度在内的连续三年',
 'three quarters through Q3 2025': '截至2025年第三季度的三个季度',
 'three years': '三年',
 'three years from launch': '自推出起三年',
 'three-year implementation period, FY2030': '三年实施期；2030财年',
 'through 2024': '至2024年',
 'through 2026': '截至2026年',
 'through 2029': '截至2029年',
 'through 2030': '至2030年',
 'through 2034': '截至2034年',
 'through end of decade': '截至本十年末',
 'through fiscal 2018': '至2018财年',
 'trailing four quarters through fiscal Q3 2026': '截至2026财年第三季度的过去四个季度',
 'trailing period reported 2025': '2025年披露的滚动期间',
 'trailing three years': '过去三年',
 'trailing three years and recent quarters': '过去三年及近期各季度',
 'trailing twelve months through Q2': '截至第二季度的过去12个月',
 'trailing twelve months through fiscal 2026 Q2, with outlook into fiscal 2027': '截至2026财年第二季度的过去十二个月，并展望2027财年',
 'trailing two years to 2025': '截至2025年的过去两年',
 'trailing year and forward': '过去一年及以后',
 "under the outgoing CEO's tenure through fiscal 2018": '截至2018财年的前任CEO任期',
 'unspecified': '未明确说明',
 'unspecified (executive biography)': '未说明（高管履历）',
 'unspecified future': '未注明的未来期间',
 'up to January 2020': '截至2020年1月',
 'upcoming next earnings call': '下一次业绩电话会',
 'year to date': '本年年初至报告期',
 'year-to-date 2025': '2025年年初至报告期',
 'year-to-date Q3 FY26': '2026财年年初至第三季度',
 'years ahead': '未来若干年'}

COMMON_CLAIM_PERIOD_LABELS = {
    "1H26": "2026年上半年",
    "1H26 to 2H26": "2026年上半年至下半年",
    "1H26-2H26": "2026年上半年至下半年",
    "Back half of fiscal year": "财年下半年",
    "DXC second half of fiscal 2026": "DXC 2026财年下半年",
    "DXC second quarter of fiscal 2026": "DXC 2026财年第二季度",
    "DXC third quarter of fiscal 2026": "DXC 2026财年第三季度",
}

FIGURE_METRIC_LABELS = {
    "metric:revenue": "营业收入",
    "metric:cc-revenue-growth": "按固定汇率计算的营收增长",
    "metric:adjusted-operating-margin": "调整后营业利润率",
    "metric:adj-operating-margin": "调整后营业利润率",
    "metric:adj-ebit-margin": "调整后 EBIT 利润率",
    "metric:ebit-margin": "EBIT 利润率",
    "metric:adjusted-eps": "调整后每股收益",
    "metric:adj-eps": "调整后每股收益",
    "metric:adjusted-diluted-eps-growth": "调整后稀释每股收益增长",
    "metric:bookings-growth": "签约额增长",
    "metric:book-to-bill-ratio": "订单收入比（book-to-bill）",
}


def _figure_display_label(metric_ref: Any, as_reported_label: Any) -> str:
    """Name a verified figure without translating its value-bearing sentence."""
    label = FIGURE_METRIC_LABELS.get(metric_ref)
    if label is not None:
        return label
    if isinstance(as_reported_label, str) and not re.search(r"[A-Za-z]", as_reported_label):
        return as_reported_label
    return "已核实指标（原始名称见技术详情）"


def claim_period_display_label(value: Any) -> str | None:
    """Render period metadata; the original value remains in technical details."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    fiscal_years = re.fullmatch(
        r"fiscal years ((?:19|20)\d{2}(?:,\s*(?:19|20)\d{2})*)"
        r",?\s+and\s+((?:19|20)\d{2})", text, flags=re.IGNORECASE)
    if fiscal_years:
        years = re.findall(r"(?:19|20)\d{2}", fiscal_years[1])
        return "、".join(years) + "及" + fiscal_years[2] + "财年"
    quarter = re.fullmatch(r"(?:(FY|CY))?(\d{4})Q([1-4])", text,
                           flags=re.IGNORECASE)
    if quarter:
        basis = {None: "", "FY": "财年", "CY": "自然年"}[
            quarter[1].upper() if quarter[1] else None]
        return f"{quarter[2]}{basis or '年'}第{'一二三四'[int(quarter[3]) - 1]}季度"
    short_quarter = re.fullmatch(r"(FY|CY)(\d{2})Q([1-4])", text,
                                 flags=re.IGNORECASE)
    if short_quarter:
        basis = "财年" if short_quarter[1].upper() == "FY" else "自然年"
        return f"20{short_quarter[2]}{basis}第{'一二三四'[int(short_quarter[3]) - 1]}季度"
    alternate_fiscal = re.fullmatch(r"F([1-4])Q(\d{2})", text,
                                    flags=re.IGNORECASE)
    if alternate_fiscal:
        return f"20{alternate_fiscal[2]}财年第{'一二三四'[int(alternate_fiscal[1]) - 1]}季度"
    date_range = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})", text)
    if date_range:
        try:
            start, end = date.fromisoformat(date_range[1]), date.fromisoformat(date_range[2])
        except ValueError:
            pass
        else:
            if start <= end:
                def shown(day: date) -> str:
                    return f"{day.year}年{day.month}月{day.day}日"
                return f"{shown(start)}至{shown(end)}"
        return "期间说明见技术详情"
    reviewed = REVIEWED_CLAIM_PERIOD_LABELS.get(
        text, COMMON_CLAIM_PERIOD_LABELS.get(text))
    if reviewed is not None:
        return reviewed
    # Longer phrases precede their component words. Word boundaries prevent
    # "report date" from corrupting "report dated", or "year" from eating "years".
    phrases = {
        "current guidance period, with reference to the Q4 call and prior one to two quarters":
            "当前指引期，参考第四季度电话会及此前一至两个季度",
        "current quarter versus prior year and prior quarter": "当前季度，与上年同期及上一季度比较",
        "current quarter with outlook over the next quarter or two": "当前季度，并展望未来一至两个季度",
        "current and historical EMEA comparison": "当前与历史欧洲、中东及非洲业务对比",
        "most recent fiscal year": "最近财年",
        "annual report year ended": "年度报告，年度截至",
        "fiscal year covered by the Form 10-K": "Form 10-K 覆盖的财年",
        "fiscal year covered by filing": "申报文件覆盖的财年",
        "forward-looking statement (current release)": "本次发布的前瞻性表述",
        "company description (current)": "当前公司概况",
        "post Kyndryl spin-off": "分拆 Kyndryl 之后",
        "current second half pipeline": "当前下半年商机储备",
        "current CEO transition": "当前首席执行官交接期",
        "current guidance period": "当前指引期",
        "current outlook period": "当前展望期",
        "current filing period": "当前申报期",
        "current company description": "当前公司概况",
        "current pipeline period": "当前商机期",
        "current rating period": "当前评级期",
        "currentratingperiod": "当前评级期",
        "current rating": "当前评级",
        "current coverage": "当前覆盖期",
        "current and medium-to-long term": "当前至中长期",
        "current and medium term": "当前及中期",
        "current to medium-term": "当前至中期",
        "current and forward-looking": "当前及前瞻期",
        "current and forward": "当前及未来",
        "current and near term": "当前及近期",
        "current and near-term": "当前及近期",
        "current and future": "当前及未来",
        "current/future": "当前及未来",
        "current quarter vs. last year": "当前季度同比",
        "current fiscal year second half": "当前财年下半年",
        "current year second half": "当前年度下半年",
        "current year second quarter": "当前年度第二季度",
        "current year and next year": "当前年度及下一年度",
        "first half of current fiscal year": "当前财年上半年",
        "first half of current year": "当前年度上半年",
        "first quarter of current fiscal year": "当前财年第一季度",
        "second half of current fiscal year and beyond": "当前财年下半年及以后",
        "second half of current fiscal year": "当前财年下半年",
        "second half of current year": "当前年度下半年",
        "second half current year": "当前年度下半年",
        "first half (fiscal year unspecified)": "上半年（财年未注明）",
        "remainder of current fiscal year": "当前财年剩余时间",
        "until early next fiscal year": "至下一财年初",
        "last couple of years and ongoing": "过去几年至今",
        "last 1-2 years and current": "过去 1–2 年至今",
        "last 6 months as of the call": "截至电话会时的过去 6 个月",
        "last months and weeks as of the call": "截至电话会时的最近几个月及几周",
        "late last year through current": "上年末至今",
        "past decade and current": "过去十年至今",
        "past year and current": "过去一年至今",
        "historical and current": "历史及当前",
        "period not specified": "期间未注明",
        "not specified": "期间未注明",
        "current commentary": "当前评论",
        "recent opportunities": "近期机会",
        "current engagement": "当前合作期间",
        "current era": "当前时期",
        "current filing": "当前申报文件",
        "current outlook": "当前展望",
        "current view": "当前判断",
        "current trend": "当前趋势",
        "current period starting July": "自 7 月开始的当前期间",
        "multi-year strategy": "多年战略",
        "trailing twelve months": "过去十二个月",
        "medium-to-long-term": "中长期",
        "medium to long term": "中长期",
        "over the medium term": "中期内",
        "medium-term guidance": "中期指引",
        "medium-term": "中期",
        "medium term": "中期",
        "long-term": "长期",
        "long term": "长期",
        "forward-looking": "前瞻期",
        "multiyear": "多年期间",
        "current/forecast to": "当前／预测至",
        "current/forecast": "当前／预测",
        "current fiscal year": "当前财年",
        "current quarter": "当前季度",
        "current period": "当前期间",
        "current year": "当前年度",
        "current as of the call": "截至电话会时",
        "current as of": "截至",
        "current into": "当前至",
        "as of the call": "截至电话会时",
        "as of": "截至",
        "next fiscal year": "下一财年",
        "fiscal years": "财年",
        "fiscal year": "财年",
        "fiscal": "财年",
        "FY guidance period": "财年指引期",
        "years ended": "各年度截至",
        "year ended": "年度截至",
        "report dated": "报告日期为",
        "report date": "报告日",
        "risk factors": "风险因素",
        "given at": "发布于",
        "reported": "披露于",
        "full-year": "全年",
        "first quarter": "第一季度",
        "second quarter": "第二季度",
        "third quarter": "第三季度",
        "fourth quarter": "第四季度",
        "52-week period up to": "截至下列日期的 52 周期间：",
        "Belarus restrictions through end of": "白俄罗斯相关限制持续至下列年份年底：",
        "current": "当前",
        "ongoing": "持续中",
        "guidance": "指引",
        "outlook": "展望",
        "actual": "实际业绩",
        "goals": "目标",
        "analysis": "分析时点",
        "report": "报告",
        "versus": "对比",
        "vs": "对比",
        "and": "及",
    }
    for phrase, replacement in phrases.items():
        text = re.sub(r"(?<![A-Za-z])" + re.escape(phrase) + r"(?![A-Za-z])",
                      lambda _: replacement, text, flags=re.IGNORECASE)
    text = re.sub(r"\bpost-(\d{4})\b", r"\1 年后", text, flags=re.IGNORECASE)
    text = re.sub(r"\bearly (\d{4})\b", r"\1 年初", text, flags=re.IGNORECASE)
    names = ("January", "February", "March", "April", "May", "June", "July",
             "August", "September", "October", "November", "December")
    months = {form.lower(): i for i, name in enumerate(names, 1)
              for form in (name, name[:3])}
    month = "(?:" + "|".join(sorted(months, key=len, reverse=True)) + r")\.?"
    def month_number(raw: str) -> int:
        return months[raw.rstrip(".").lower()]
    text = re.sub(r"\b(" + month + r")\s+(\d{1,2}),\s+(\d{4})\b",
        lambda m: f"{m[3]}年{month_number(m[1])}月{int(m[2])}日", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(\d{1,2})\s+(" + month + r")\s+(\d{4})\b",
        lambda m: f"{m[3]}年{month_number(m[2])}月{int(m[1])}日", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(" + month + r")\s+(\d{4})\b",
        lambda m: f"{m[2]}年{month_number(m[1])}月", text, flags=re.IGNORECASE)
    text = re.sub(
        r"^(\d+) months ending (" + month + r")\s+(\d{1,2})$",
        lambda m: f"截至{month_number(m[2])}月{int(m[3])}日的 {m[1]} 个月",
        text, flags=re.IGNORECASE)
    def half_year(raw_half: str, raw_year: str) -> str:
        year = int(raw_year)
        if year < 100:
            year += 2000
        return f"{year}年{'\u4e0a' if raw_half == '1' else '\u4e0b'}半年"
    text = re.sub(
        r"\b([12])H(\d{2,4})\b",
        lambda m: half_year(m[1], m[2]), text, flags=re.IGNORECASE)
    text = re.sub(
        r"\b(\d{4})\s+(?:through|to)\s+first half of\s+(\d{4})\b",
        lambda m: f"{m[1]}年至{m[2]}年上半年", text, flags=re.IGNORECASE)
    text = re.sub(
        r"\b(\d{4})\s+(?:through|to)\s+(\d{4})\b",
        lambda m: f"{m[1]}年至{m[2]}年", text, flags=re.IGNORECASE)
    text = re.sub(
        r"\b(DXC) second quarter of fiscal (\d{4})\b",
        lambda m: f"{m[1]} {m[2]}财年第二季度", text, flags=re.IGNORECASE)
    text = re.sub(r"^Back half of fiscal year$", "财年下半年", text,
                  flags=re.IGNORECASE)
    text = re.sub(r"截至下列日期的 52 周期间： (\d{4}年\d{1,2}月\d{1,2}日)",
                  r"截至 \1 的 52 周期间", text)
    text = re.sub(r"白俄罗斯相关限制持续至下列年份年底： (\d{4})",
                  r"白俄罗斯相关限制持续至 \1 年底", text)
    text = re.sub(r"(?<!各)年度截至 (\d{4}年\d{1,2}月\d{1,2}日)",
                  r"截至 \1 的年度", text)
    quarter_names = "一二三四"
    def full_year(raw: str) -> str:
        return raw if len(raw) == 4 else f"20{raw}"
    # This function receives period metadata, never research prose. Normalize
    # complete accounting-period tokens after the descriptive phrases above;
    # the raw period remains alongside ``period_label`` in every API payload.
    rules = (
        (r"(?:财年|FY)\s*((?:20\d{2}|\d{2}))\s*Q([1-4])",
         lambda m: f"{full_year(m[1])}财年第{quarter_names[int(m[2])-1]}季度"),
        (r"Q([1-4])\s*(?:财年|FY)\s*((?:20\d{2}|\d{2}))",
         lambda m: f"{full_year(m[2])}财年第{quarter_names[int(m[1])-1]}季度"),
        (r"(?:财年|FY)\s*Q([1-4])\s*((?:20\d{2}|\d{2}))",
         lambda m: f"{full_year(m[2])}财年第{quarter_names[int(m[1])-1]}季度"),
        (r"(\d{4})\s+Q([1-4])",
         lambda m: f"{m[1]}年第{quarter_names[int(m[2])-1]}季度"),
        (r"F([1-4])Q((?:20\d{2}|\d{2}))",
         lambda m: f"{full_year(m[2])}财年第{quarter_names[int(m[1])-1]}季度"),
        (r"([1-4])Q((?:20\d{2}|\d{2}))",
         lambda m: f"{full_year(m[2])}年第{quarter_names[int(m[1])-1]}季度"),
        (r"(?:财年|FY)\s*((?:20\d{2}|\d{2}))",
         lambda m: f"{full_year(m[1])}财年"),
        (r"(?:自然年|CY)\s*((?:20\d{2}|\d{2}))",
         lambda m: f"{full_year(m[1])}自然年"),
        (r"Q([1-4])\s+(\d{4})",
         lambda m: f"{m[2]}年第{quarter_names[int(m[1])-1]}季度"),
        (r"F([1-4])Q(?!\d)",
         lambda m: f"财年第{quarter_names[int(m[1])-1]}季度"),
        (r"([1-4])Q(?!\d)",
         lambda m: f"第{quarter_names[int(m[1])-1]}季度"),
        (r"Q([1-4])",
         lambda m: f"第{quarter_names[int(m[1])-1]}季度"),
    )
    for pattern, replacement in rules:
        text = re.sub(r"(?<![A-Za-z0-9])" + pattern + r"(?![A-Za-z0-9])",
                      replacement, text, flags=re.IGNORECASE)
    # A partial phrase match must not produce invented mixed-language metadata.
    # Keep familiar financial period notation and proper names, but return an
    # unrecognised period verbatim rather than translate only its generic words.
    remaining = re.sub(
        r"\b(?:Form 10-K|Kyndryl|TTM|NTM|LTM|FY\d{4}Q[1-4]|FY\d*|CY\d*|F?Q[1-4]\d*|F?[1-4]Q\d*)\b",
        "", text, flags=re.IGNORECASE)
    # A period can be useful even when its source used an unfamiliar phrase,
    # but partially translating that phrase would invent a meaning.  Keep the
    # exact source bytes in ``period`` and show this closed Chinese fallback.
    return "期间说明见技术详情" if re.search(r"[A-Za-z]", remaining) else text


# Compatibility for callers/tests written before the display contract was
# made reusable by other read-only renderers.
_claim_period_label = claim_period_display_label


def _answer_citation_period_labels(result: Any) -> Any:
    """Add display-only period labels without mutating a stored answer."""
    if not isinstance(result, Mapping):
        return result
    public = dict(result)
    citations = result.get("citations")
    if not isinstance(citations, list):
        return public
    shown: list[Any] = []
    for citation in citations:
        if not isinstance(citation, Mapping):
            shown.append(citation)
            continue
        row = dict(citation)
        label = _claim_period_label(row.get("period"))
        if label is not None:
            row["period_label"] = label
        shown.append(row)
    public["citations"] = shown
    return public


CONNECTION_STATUS_LABELS = {
    "connected": "已连接", "not_connected": "尚未连接",
    "probe_only": "仅允许试读", "undeclared": "研究目标尚未声明该来源",
    "unknown": "状态不明",
}
COMPLETENESS_LABELS = {
    "enumerated": "可完整获取", "bounded": "可获取限定范围", "sampled": "仅能获取样本",
}
# The tracking policy's source keys, named for the owner. A key with no name
# here shows its key, which is ugly and visible -- the same rule the lane
# panel follows.
TRACKING_SOURCE_LABELS = {
    "yfinance": "股价", "sec": "SEC 报表与 8-K", "alphaengine": "卖方研报与纪要",
    "x-xreach": "X（推特）", "sales-notes": "卖方销售简报",
    "gemini-web-search": "公开网页搜索", "guidepoint": "专家访谈",
    "company-wiki": "内部公司知识库", "employee-reviews": "员工评价",
    "catalyst-calendar": "催化剂日历",
}
CATALYST_EVENT_LABELS = {
    "earnings": "业绩发布", "guidance": "指引", "investor_day": "投资者日",
    "filing_due": "报表到期", "ex_dividend": "除息日", "other": "其他",
}
# P14e: what a special-purpose research task ended up as.
RESEARCH_TASK_STATE_LABELS = {
    "admitted": "已排队，等待执行", "running": "正在执行", "terminal": "已结束",
}
RESEARCH_TASK_TERMINAL_LABELS = {
    "evidence_observed_for_review": "有发现，待复核",
    "coverage_complete_unobservable_candidate": "查遍了，没有可观察到的证据",
    "budget_exhausted": "预算已用完，研究尚未完成",
    "human_replan_required": "等待人工重新规划",
    "human_deprioritized": "人工降低了优先级",
}
# The two model tiers a purpose can sit in, and what each is for.
# P14-M2: follow the configured tier or explicitly select models; the third
# describes what a stage does when every explicitly selected model
# model the owner named has been retired.
MODEL_SELECTION_MODE_LABELS = {
    "tier": "使用系统推荐配置",
    "explicit": "手动指定模型",
    "tier_after_retirement": "指定模型已退役，暂时使用该环节的默认模型顺序",
    "legacy_pin": "沿用当前策略固定的模型",
    "policy_filters": "由当前策略筛选模型",
    "tier_preview": "默认档位预览（尚未绑定策略）",
}
WORK_STATE_LABELS = {
    "ready": "排队", "leased": "开始执行", "succeeded": "完成",
    "failed": "失败", "deferred": "延后", "cancelled": "取消",
}
SKIP_REASON_LABELS = {
    "budget_refused": "当时预算未放行",
    "profile_retired": "模型已下线",
    "profile_not_allowed": "该环境未获授权使用此模型",
    "credential_slot_unavailable": "对应渠道凭证暂不可用",
    "capability_not_supported": "模型能力不符",
    "profile_not_available": "网关当前不提供",
    "unknown_send": "发送状态未知，已保守跳过",
    "verify_not_independent": "与产出方同家族，不能复核",
}
BUDGET_REASON_LABELS = {
    "owner_budget_exceeded": "当日总预算已用完",
    "mission_budget_exceeded": "任务当日额度已用完",
    "outer_research_budget_exceeded": "跨任务当日额度已用完",
    "pool_exhausted": "分项预算池已用完",
}
MODEL_TIER_LABELS = {
    "brain": "高阶推理与规划",
    "cheap": "批量阅读与整理",
    "verifier": "独立复核",
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
# P12d: the three words the gate authority accepts, so a button cannot offer
# something the writer would refuse.
GATE_ACTIONS = (
    {"decision": "approve", "label": "通过"},
    {"decision": "return_for_more_work", "label": "退回补充"},
    {"decision": "reject", "label": "否决"},
)


def _gate_answer_line(item: Mapping[str, Any]) -> str:
    """One gate answer as one line of text, for the details renderer."""

    from .deep_insight_gate import answer_body

    head = str(item["question"])
    if item["status"] == "answered":
        refs = len(item["sources"])
        return (f"{head} —— {answer_body(item)}"
                f"（置信度 {CONFIDENCE_LABELS.get(item['confidence'], item['confidence'])}，{refs} 条引用）")
    unknown = item["unknown"]
    return (f"{head} —— 未回答：{unknown['missing']}。"
            f"所需证据：{unknown['evidence_that_would_answer']}")


def _gate_decidability(core: sqlite3.Connection, record: Mapping[str, Any]) -> dict[str, Any]:
    """Whether the stage ladder would accept this gate's decision today.

    One predicate, shared with the writer operation behind the button, so the
    page and the door can never disagree about whether it opens.
    """

    from .deep_insight_gate import decidability

    return decidability(core, record)


CHECKPOINT_TITLES = {
    "thesis_revision_candidate": "新证据可能影响现有投资论点，待人工决策",
    "gate_reopen": "已有研究报告需要重新评估",
}
APPROVAL_DETAIL_LABELS = {
    "thesis": {"信心": "置信度", "提议者": "提议人"},
    "capability": {"已有评估": "是否已有评估"},
    "planner": {"轮次": "研究轮次"},
    "forecast": {"偏离": "实际值与预测的偏离"},
    "deep_insight_gate": {"行业分类": "行业分类"},
    "investment_memo": {"独立核验": "独立核验结果"},
    "claim": {"发现的问题": "需要处理的问题", "依据": "判断依据"},
    "conviction_call": {
        "我们的看法": "当前投资判断", "市场的看法": "市场一致预期",
        "时间跨度": "投资期限", "信心": "置信度",
        "风险回报是否达标": "风险回报标准", "怎么裁决": "所需决定",
    },
    "thesis_revision_candidate": {
        "大脑的判断": "系统研判", "提议改成": "建议修订为",
        "提议的把握": "建议置信度", "修订原因": "修订原因",
        "此前通过时间": "此前通过时间",
    },
    "gate_reopen": {
        "大脑的判断": "系统研判", "提议改成": "建议修订为",
        "提议的把握": "建议置信度", "修订原因": "修订原因",
        "此前通过时间": "此前通过时间",
    },
    "forecast_proposal": {
        "哪一期": "预测期间", "想改成": "建议值",
        "为什么没直接改": "需要人工决定的原因",
    },
    "model_fallback": {"环节": "受影响的研究环节", "档位": "模型档位"},
}
# What each one's buttons say, when this Core can actually decide it. The
# words are the authorities' own verdict vocabularies -- ``accept / reject /
# defer`` (P14b) and ``approve / decline`` (P14d) -- so a button cannot offer
# something the writer would refuse.
CHECKPOINT_ACTIONS = {
    "thesis_revision_candidate": (
        {"decision": "accept", "label": "接受，出新版本"},
        {"decision": "reject", "label": "不接受"},
        {"decision": "defer", "label": "暂不处理"},
    ),
    "gate_reopen": (
        {"decision": "approve", "label": "批准修订"},
        {"decision": "decline", "label": "保留当前版本"},
    ),
}
# And what it says instead, on a Core whose writer predates the decision ops.
CHECKPOINT_UNDECIDABLE_NOTES = {
    "thesis_revision_candidate": "需要人工决策；当前版本尚不能记录这类决定",
    "gate_reopen": "需要人工决策；当前版本尚不能记录这类决定",
}
# C2: the four pools a day's budget is split into, named for what each buys.
POOL_LABELS = {
    "coverage": "公司持续研究",
    "event_response": "事件响应",
    "adhoc": "专项研究",
    "maintenance": "研究维护",
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
COCKPIT_DEFAULT_MAX_COST_USD = 2.0
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


class CockpitMissionMissing(CockpitError):
    """An initialized workspace has no published research mandate yet."""


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
        "此前通过的版本": (None if passed_ref is None
                         else f"v{record.get('passed_version_number')}（{passed_ref}）"),
        "此前通过时间": record.get("passed_at"),
        "修订原因": CHANGE_REASON_LABELS.get(
            record.get("change_reason"), record.get("change_reason")),
        "发生退步的项目": list(record.get("regressed") or ()),
    }
    erratum = record.get("erratum") or {}
    substitutions = erratum.get("substitutions") if isinstance(erratum, Mapping) else None
    corrections = [
        f"{change.get('before')} → {change.get('after')}"
        for change in substitutions or ()
        if isinstance(change, Mapping) and change.get("before") and change.get("after")
    ]
    if (record.get("change_reason") == "human_revision"
            and record.get("policy_ref") == "owner-directed-factual-erratum:0.1"
            and corrections):
        display_summary = "根据已核验来源纠正事实：" + "；".join(corrections)
        details["修订原因"] = "根据已核验来源纠正事实"
    elif flipped:
        display_summary = "；".join(flipped)
    elif summary:
        display_summary = summary
    elif record.get("change_reason") == "human_revision":
        display_summary = "人工修订记录未提供具体纠错摘要。"
    else:
        display_summary = "原先缺失的证据现已补齐。"
    return display_summary, details


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
    workspace_manager_config_path: Path | None = None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CockpitConfig":
        fields = {"core_db", "state_dir", "heartbeat_path", "scheduler_db", "journal_path",
                  "model_config_path", "mission_ref", "openclaw_config_path", "workspace_manager_config_path"}
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
            workspace_manager_config_path=(None if raw.get("workspace_manager_config_path") is None
                else _path(raw["workspace_manager_config_path"], "workspace_manager_config_path")),
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
        from .workspace_cockpit import cockpit_workspace_context
        # Validate the process namespace before opening even the local journal.
        self.workspace_context = cockpit_workspace_context(
            config, writer_socket=writer_socket, token_config=token_config)
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
        self._overview_condition = threading.Condition()
        self._overview_building = False
        self._overview_generation = 0
        self._overview_result: dict[str, Any] | None = None
        self._lane_governance_cache: dict[tuple[str, str], str | None] = {}
        # Prime the two immutable/read-only indexes while the control service
        # starts.  The first browser request should project current state, not
        # spend seconds parsing ten thousand historical ticket files and the
        # already hash-bound localization store.
        try:
            primed_tickets = self.tickets.tickets()
        except OSError:
            # Prewarming is an optimization.  A transient filesystem error
            # must not turn control-service startup into an outage; the first
            # request retains the existing fresh-read behavior.
            primed_tickets = []
        try:
            from .research_localization_store import load_ui_texts
            load_ui_texts(self.config.core_db)
        except (ImportError, OSError, sqlite3.Error, ValueError, TypeError):
            pass
        if len(primed_tickets) > 1_000:
            try:
                from .lane_registry import LaunchAgentContext, registered_lanes
                context = LaunchAgentContext(state=self.config.state_dir)
                for spec in registered_lanes():
                    self._lane_governance_record(spec, context)
            except (ImportError, OSError, ValueError, TypeError):
                pass

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
            raise CockpitMissionMissing("no research goal has been published yet")
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

    def _url_map(self, tickets: Sequence[Mapping[str, Any]] | None = None
                 ) -> dict[str, dict[str, Any]]:
        """document_ref → {url, host, title} from fetch tickets and search summaries."""
        tickets = list(tickets) if tickets is not None else self.tickets.tickets()
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
                "display_label": _figure_display_label(
                    row["metric_ref"], row["as_reported_label"]),
                "period": row["period"],
                "period_label": _claim_period_label(row["period"]),
                "value": row["value"], "unit": row["unit"],
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
            changes = []
            for trading_days in (5, 30, 90):
                # N-day performance has N close-to-close intervals, hence it
                # needs N+1 trading-day observations. Calendar gaps do not
                # count as missing sessions in an exchange close series.
                item = {"days": trading_days, "horizon": f"{trading_days}D",
                        "label": f"近{trading_days}个交易日"}
                if len(bars) <= trading_days:
                    item.update({
                        "status": "unavailable", "percent": None,
                        "since": None,
                        "reason": f"历史收盘数据不足，暂时无法计算{trading_days}个交易日涨跌幅",
                    })
                else:
                    comparison = bars[-(trading_days + 1)]
                    try:
                        percent = round(
                            (float(newest["close"]) / float(comparison["close"]) - 1)
                            * 100, 1)
                    except (TypeError, ValueError, ZeroDivisionError):
                        item.update({
                            "status": "unavailable", "percent": None,
                            "since": None,
                            "reason": f"历史收盘数据无效，暂时无法计算{trading_days}个交易日涨跌幅",
                        })
                    else:
                        item.update({
                            "status": "available", "percent": percent,
                            "since": comparison["date"], "reason": None,
                        })
                changes.append(item)
            provisional = bar_is_provisional(newest)
            out[record["company_ref"]] = {
                "as_of": newest["date"], "close": newest["close"],
                "adj_close": newest["adj_close"], "currency": record.get("currency"),
                "provisional": provisional,
                "note": ("这是盘中价，当天还没有收盘定价"
                         if provisional else "收盘价"),
                "bars": len(bars), "since": record.get("first_bar_date"),
                "change_percent": change, "change_since": first["date"],
                "changes": changes,
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

    def _invariants(self, core: Any) -> dict[str, dict[str, Any]]:
        """P17b: which outputs are missing because an economic invariant refused.

        Read from the head of each verdict chain rather than from a count of
        refusals: what a reader needs is not how often the gate fired, it is
        whether the number they came to look at is absent right now and which
        sentence says why. A chain whose head is a pass is not shown, because
        an output that is there does not need a note saying it was allowed.
        """

        if not _table_exists(core, "economic_invariant_verdicts"):
            return {}
        from .economic_invariants import (
            INVARIANT_LABELS, NOT_APPLICABLE, OUTPUT_KIND_LABELS, UNAVAILABLE,
        )

        out: dict[str, dict[str, Any]] = {}
        for row in self._rows(core,
            "SELECT v.* FROM economic_invariant_verdicts v JOIN ("
            "SELECT verdict_ref, MAX(version_number) AS top "
            "FROM economic_invariant_verdicts GROUP BY verdict_ref) h "
            "ON v.verdict_ref=h.verdict_ref AND v.version_number=h.top "
            "WHERE v.status=? ORDER BY v.company_ref, v.output_kind",
            (UNAVAILABLE,),
        ):
            record = json.loads(row["record_json"])
            kind = str(record.get("output_kind"))
            out.setdefault(str(record.get("company_ref")), {})[kind] = {
                "output_kind": kind,
                "output_kind_label": OUTPUT_KIND_LABELS.get(kind, kind),
                "output_ref": record.get("output_ref"),
                "status": record.get("status"),
                "refused_at": record.get("created_at"),
                "rule_ref": record.get("rule_ref"),
                "reasons": list(record.get("reasons") or []),
                "failed": [{
                    "invariant": item.get("invariant"),
                    "label": INVARIANT_LABELS.get(
                        str(item.get("invariant")), item.get("invariant")),
                    "findings": list(item.get("findings") or []),
                } for item in (record.get("results") or [])
                    if item.get("status") == "fail"],
                "not_checked": [],
                "note": "这个产出没有发布：它没通过经济不变量检查，理由在下面逐条列出",
            }
        # Successful model versions retain the exact report evaluated before
        # publication. Surface its not-applicable checks without adding them
        # to the refusal authority (whose chain deliberately records only
        # refusals and their clearings).
        if (_table_exists(core, "forecast_model_versions")
                and _table_exists(core, "forecast_model_filing_proofs")):
            for row in self._latest_by(
                core, "forecast_model_versions", "model_ref", "version_number"
            ):
                model = json.loads(row["record_json"])
                proof_row = core.execute(
                    "SELECT record_json FROM forecast_model_filing_proofs "
                    "WHERE model_version_id=?", (row["version_id"],)
                ).fetchone()
                if proof_row is None:
                    continue
                proof = json.loads(proof_row["record_json"])
                report = proof.get("invariant_report") or {}
                company_ref = str(model.get("company_ref"))
                kind = str(report.get("output_kind"))
                if not kind or kind in (out.get(company_ref) or {}):
                    continue
                not_checked = [{
                    "invariant": item.get("invariant"),
                    "label": INVARIANT_LABELS.get(
                        str(item.get("invariant")), item.get("invariant")),
                    "findings": ([str(item.get("reason"))]
                                 if item.get("reason") else []),
                } for item in (report.get("results") or [])
                    if item.get("status") == NOT_APPLICABLE]
                if not not_checked:
                    continue
                out.setdefault(company_ref, {})[kind] = {
                    "output_kind": kind,
                    "output_kind_label": OUTPUT_KIND_LABELS.get(kind, kind),
                    "output_ref": report.get("output_ref"),
                    "status": report.get("status"),
                    "reasons": [], "failed": [], "not_checked": not_checked,
                    "note": "这个产出没有经济不变量失败，但下列检查因证据不足而未执行",
                }
        from .cockpit_model_display import present_invariants
        return present_invariants(out)

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
            return head + f"；仍有 {len(missing)} 项结果缺少计算条件"
        partial = len(readiness.get("results_partial") or [])
        return head + (f"；另有 {partial} 项仅完成部分计算" if partial else "；各项结果已完成计算")

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
        now = self.clock()
        if now.tzinfo is None: now = now.replace(tzinfo=timezone.utc)
        utc_day = now.astimezone(timezone.utc).date()
        today = utc_day.isoformat()
        tomorrow = (utc_day + timedelta(days=1)).isoformat()
        for row in self._rows(core,
            "SELECT company_ref, kind, evidence_tier, occurred_at, record_json "
            "FROM research_events WHERE datetime(occurred_at)>=datetime(?) "
            "AND datetime(occurred_at)<datetime(?) ORDER BY occurred_at, event_id",
            (today + "T00:00:00+00:00", tomorrow + "T00:00:00+00:00"),
        ):
            entry = out.setdefault(row["company_ref"], {
                "total": 0, "by_kind": {}, "latest": [],
                "date": today, "date_basis": "utc_calendar_date",
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
                                else "当前没有可供比较的市场一致预期"),
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
        """Build one overview at a time and share it with concurrent readers.

        Browsers can overlap the initial request with polling or a retry.  The
        overview performs several bounded but substantial read projections;
        running identical projections concurrently makes each one contend for
        the Python GIL and SQLite page cache.  A caller that arrived while a
        build was active receives that exact completed snapshot.  A later,
        sequential request still builds afresh, preserving the existing
        read-after-write behavior.
        """

        with self._overview_condition:
            observed_generation = self._overview_generation
            while self._overview_building:
                self._overview_condition.wait()
                if (self._overview_generation > observed_generation
                        and self._overview_result is not None):
                    return self._overview_result
            self._overview_building = True
        try:
            result = self._build_overview()
        except BaseException:
            with self._overview_condition:
                self._overview_building = False
                self._overview_condition.notify_all()
            raise
        with self._overview_condition:
            self._overview_result = result
            self._overview_generation += 1
            self._overview_building = False
            self._overview_condition.notify_all()
        return result

    def _build_overview(self) -> dict[str, Any]:
        # UI translations are a read-only adjunct to authority text. Older
        # installations retain their exact payload when the cache is absent.
        try:
            from .research_localization_store import load_ui_texts
            text_localizations = load_ui_texts(self.config.core_db)
            if len(json.dumps(text_localizations, ensure_ascii=False).encode()) > 2_000_000:
                text_localizations = {}
        except (ImportError, OSError, sqlite3.Error, ValueError, TypeError):
            text_localizations = {}
        language_review_required = False
        try:
            policy = _load_json(self.config.core_db.parent / "research-language-policy.json")
            language_review_required = (isinstance(policy, Mapping)
                                        and policy.get("required") is True)
        except (OSError, ValueError, TypeError):
            language_review_required = False
        heartbeat = _load_json(self.config.heartbeat_path) or {}
        with self._core() as core:
            try:
                mission = self._mission(core)
            except CockpitMissionMissing:
                saved = self.journal.rows(
                    "SELECT draft_id,draft_json,content_hash,created_at,status FROM cockpit_drafts "
                    "WHERE kind='goal' AND status IN ('saved','open') ORDER BY created_at DESC LIMIT 1")
                initial = None if not saved else {
                    "draft_id": saved[0]["draft_id"], "draft_hash": saved[0]["content_hash"],
                    "kind": "goal", "status": saved[0]["status"], "created_at": saved[0]["created_at"],
                    "draft": json.loads(saved[0]["draft_json"])}
                return {"schema_version": SCHEMA_VERSION,
                        "as_of": _iso(self.clock()),
                        "state": "awaiting_mission",
                        "workspace": self.workspace_context,
                        "initial_goal": initial, "goal": None}
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
            invariants = self._invariants(core)
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
        generation_failures = _latest_initial_screen_failures(self.config.state_dir)
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
            stage_readiness = _stage_readiness_labels({
                **entry,
                "latest_generation_failure": generation_failures.get(company_ref),
            })
            if entry["stage"] is None:
                note = "还没有开始"
            elif missing:
                note = "还差：" + "、".join(f"{i['label']}（{i['have']}/{i['required']}）" for i in missing[:3])
            elif blocked:
                note = "能拿到的资料齐了；" + blocked[0]["note"]
            else:
                note = "资料底座齐了，等着起草初步研究筛查报告"
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
                "company_ref": company_ref, "ticker": member.get("ticker"),
                "name": (member.get("name") or COMPANY_NAMES.get(member.get("ticker", ""), "")
                         or member.get("ticker") or company_ref.rsplit(":", 1)[-1]),
                "priority": member.get("bootstrap_priority"), "tier": member.get("coverage_tier"),
                "stage": entry["stage_label"], "stage_ref": entry["stage"],
                "stage_status": entry["stage_status_label"], "note": note,
                **stage_readiness,
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
                # P17b: the outputs that are absent from the three cards above
                # because an economic invariant refused them, each with the
                # reasons. An empty dict is the normal case and reads as one.
                "invariants": invariants.get(company_ref) or {},
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
        tickets = self.tickets.tickets()
        urls = self._url_map(tickets)
        running = [self._ticket_event(t, members, urls) for t in tickets
                   if _ticket_still_running(t["ticket"])]
        lane_rows = self._lane_states(
            heartbeat, extraction, discovery, mission["budget"], planner)
        # P17d 四格. Computed here, inside the one connection the page already
        # holds, so the landing page stays a single fetch and the tiles read
        # the same rows the sections below them read.
        with self._core() as ops_core:
            ops = {
                "lanes": self._panel_lanes(lane_rows),
                "gaps": self._panel_gaps(ops_core, members),
                "failures": self._panel_failures(),
                "acceptance": self._panel_acceptance(ops_core),
            }
        return {
            "schema_version": SCHEMA_VERSION, "as_of": _iso(self.clock()),
            "workspace": self.workspace_context,
            "goal": {
                "mission_ref": mission["mission_ref"], "version": mission["version"], "id": mission["id"],
                "hash": mission["content_hash"], "title": mission["title"], "objective": mission["objective"],
                "budget": dict(mission["budget"]),
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
                            for s in mission["source_plan"]
                            if s["source_ref"] != "source:company-ir"],
                # IR pages describe company-owned web material, not a
                # separately installed source. Preserve the original task
                # declaration for inspection without presenting a phantom connector.
                "source_plan_details": list(mission["source_plan"]),
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
                "lanes": lane_rows,
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
            # P17d 四格: 运行状态 / 来源缺口 / 悬置失败 / 上周产物验收, side by
            # side. Chem's health page said OK while research gaps sat unfilled
            # and a task had been permanently suspended for a week, because
            # those four facts lived on four pages. They are one row now.
            "ops": ops,
            # Q1: whether this Core can take the feedback buttons at all. A
            # Core with no journal table shows no buttons rather than buttons
            # that fail when pressed.
            "feedback_enabled": journal["enabled"],
            "model_available": self._model_status(),
            "text_localizations": text_localizations,
            "publication_policy": {"language_review_required": language_review_required},
        }

    def _stage_rows(self, core: sqlite3.Connection, mission: Mapping[str, Any]) -> list[dict[str, Any]]:
        """P10a:每家公司的阶段与资料底座清单，全部从任务自己的表里数出来。"""

        # P14-S: every version of this mission_ref, in time order. Scoped to
        # the active version, the whole page reset itself on every publish --
        # live, v7..v13 each hold their own copy of the same five ``entered``
        # rows, and only v13 holds the four ``gate_passed``.
        #
        # P14d sequel: the reopen markers fold in beside the records, in one
        # time order. Without them a company whose passed screen a person has
        # re-opened would read "已通过" on this page while the lane was
        # drafting its replacement -- the one state the page exists to show.
        state: dict[str, dict[str, list[str]]] = {}
        ordered = [
            (row["created_at"], row["record_id"], row["company_ref"], row["stage_ref"],
             row["status"])
            for row in self._rows(core,
                "SELECT company_ref, stage_ref, status, created_at, record_id "
                "FROM coverage_mission_stage_records "
                "WHERE mission_version_ref IN (SELECT mission_version_id FROM "
                "coverage_mission_versions WHERE mission_ref=?)",
                (mission["mission_ref"],),
            )
        ] + [
            (row["created_at"], row["record_id"], row["company_ref"], row["stage_ref"],
             STAGE_REOPENED)
            for row in self._rows(core,
                "SELECT company_ref, stage_ref, created_at, record_id "
                "FROM coverage_mission_stage_reopens "
                "WHERE mission_version_ref IN (SELECT mission_version_id FROM "
                "coverage_mission_versions WHERE mission_ref=?)",
                (mission["mission_ref"],),
            )
        ]
        for _at, _id, company_ref, stage_ref, status in sorted(ordered):
            state.setdefault(company_ref, {}).setdefault(stage_ref, []).append(status)
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

    @staticmethod
    def _deliverable_display_product(record: Mapping[str, Any],
                                     mission: Mapping[str, Any]) -> dict[str, Any]:
        """Rebuild the exact library projection used to review one screen."""
        bound = record.get("mission_version_ref")
        return {
            "kind": "initial_screen", "label": "初步筛选",
            "status": "available", "subject_ref": record["subject_ref"],
            "version_ref": record["id"], "content_hash": record["content_hash"],
            "created_at": record["created_at"], "mission_version_ref": bound,
            "mission_binding": "current" if bound == mission["id"] else "historical",
            "sections": [
                {"title": section["title"], "body": section.get("body") or "",
                 "sources": list(section.get("claim_refs") or []),
                 "numbers": list(section.get("numbers") or []),
                 "gaps": list(section.get("gaps") or [])}
                for section in record.get("sections") or []
            ],
            "gaps": list(record.get("gaps") or []),
            "approval": {"status": "not_applicable"},
        }

    def _localized_deliverable(self, core: sqlite3.Connection,
                               record: Mapping[str, Any],
                               mission: Mapping[str, Any]) -> dict[str, Any]:
        """Select reviewed prose for this exact version, or hide its prose."""
        from .research_localization_store import (
            directory_for_connection, has_reviewed_attachment, localize_library)

        product = self._deliverable_display_product(record, mission)
        root = directory_for_connection(core)
        if root is None or not has_reviewed_attachment(root, product):
            return {**dict(record), "sections": [],
                    "publication_status": "pending_language_review",
                    "display_reason": "正文正在进行语言检查，完成后会在这里显示。"}
        shown = localize_library(core, {"products": [product]})["products"][0]
        if (shown.get("publication_status") != "ready"
                or not isinstance(shown.get("sections"), list)):
            return {**dict(record), "sections": [],
                    "publication_status": "pending_language_review",
                    "display_reason": "正文正在进行语言检查，完成后会在这里显示。"}
        from .research_gap_display import display_metadata_text, gap_display_text
        sections = []
        for original, display in zip(record.get("sections") or [], shown["sections"]):
            sections.append({**original, "title": display["title"],
                             "body": display["body"],
                             "display_body": display_metadata_text(display["body"]),
                             "gaps": list(display.get("gaps") or []),
                             "display_gaps": [gap_display_text(gap)
                                              for gap in display.get("gaps") or []]})
        return {**dict(record), "sections": sections,
                "publication_status": "ready",
                "localization": shown.get("localization")}

    def document(self, version_ref: str) -> dict[str, Any]:
        """One deliverable, in full, for reading."""

        ref = _text(version_ref, "version_ref", maximum=512)
        with self._core() as core:
            rows = self._rows(core,
                "SELECT record_json, content_hash FROM mission_deliverable_versions WHERE version_id=?", (ref,))
            if not rows:
                raise CockpitError("这份文档不存在")
            record = json.loads(rows[0]["record_json"])
            if (record["content_hash"] != rows[0]["content_hash"]
                    or content_hash({k: v for k, v in record.items()
                                     if k != "content_hash"}) != rows[0]["content_hash"]):
                raise CockpitConflict("文档记录与哈希不符")
            mission = self._mission(core)
            record = self._localized_deliverable(core, record, mission)
            members = self._members(mission)
            # INT1: the reason a gate passed or failed lives inside the stage
            # record, not in a column. Selecting it as one made this whole page
            # raise "no such column: rationale" the first time a deliverable
            # existed to open -- which is why nothing had noticed.
            # P14-S: the history of this company's screen across every
            # version of the mission, with the version each entry was written
            # under carried as provenance -- that is what makes the list
            # readable as a history rather than as a fragment of one.
            stage = [
                {"status": row["status"],
                 "rationale": json.loads(row["record_json"]).get("rationale"),
                 "at": row["created_at"],
                 "mission_version_ref": row["mission_version_ref"]}
                for row in self._rows(core,
                    "SELECT status, record_json, created_at, mission_version_ref "
                    "FROM coverage_mission_stage_records WHERE mission_version_ref IN "
                    "(SELECT mission_version_id FROM coverage_mission_versions WHERE mission_ref="
                    "(SELECT mission_ref FROM coverage_mission_versions WHERE mission_version_id=?)) "
                    "AND company_ref=? AND stage_ref='initial_screen' "
                    "ORDER BY created_at, record_id",
                    (record["mission_version_ref"], record["subject_ref"]))
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
                    "SELECT a.reserved_micros, COALESCE(c.corrected_micros,s.actual_micros) AS settled FROM thesis_impact_day_admissions a "
                    "JOIN model_mission_budget_bindings b ON b.admission_id=a.admission_id "
                    "LEFT JOIN thesis_impact_day_settlements s ON s.admission_id=a.admission_id "
                    "LEFT JOIN thesis_impact_settlement_corrections c ON c.admission_id=a.admission_id "
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
        tell those apart from one total. Read from the day ledger, not from the
        Core, and empty on a ledger that has not been migrated -- a read-only
        copy from before C2 has no ``pool`` column at all.

        2026-09-16: pools the owner has switched off are not reported at all.
        They kept rendering "上限/还剩" numbers that no longer refuse anything,
        which reads as a live budget the system is ignoring.
        """

        if str((mission.get("budget") or {}).get("pools_enforcement") or "on") == "off":
            return None
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
            remaining = int(entry["remaining_micros"])
            currently_exhausted = remaining <= 0
            had_rejections = bool(entry["exhausted"])
            pools.append({
                "pool": name, "label": POOL_LABELS.get(name, name),
                "cap_usd": round(entry["cap_micros"] / 1_000_000, 4),
                "spent_usd": round(entry["spent_micros"] / 1_000_000, 4),
                "remaining_usd": round(remaining / 1_000_000, 4),
                "borrowed_usd": round(entry["borrowed_micros"] / 1_000_000, 4),
                "borrowed_from": [POOL_LABELS.get(key, key)
                                  for key in entry["borrowed_from"]],
                "lent_usd": round(entry["lent_micros"] / 1_000_000, 4),
                "exhausted": currently_exhausted,
                "had_rejections": had_rejections,
                "note": (
                    "这一池今天已经没有余额，新的请求会被拒"
                    if currently_exhausted else
                    "今天曾有请求因当时预算准入不足而未执行；当前仍有余额"
                    if had_rejections else None
                ),
            })
        return {
            "day": status["day"], "pools": pools,
            # A default split is not the owner's split. Saying so is the
            # difference between a number they chose and one they inherited.
            "caps_defaulted": status["caps_defaulted"],
            "caps_note": ("这四个上限是默认分法，研究目标里没有自己的分法"
                          if status["caps_defaulted"] else "上限来自研究目标自己的分法"),
            "borrow_open": status["borrow_open"],
            "borrow_note": ("半日后可将未使用的预算配额调拨至覆盖研究预算池"
                            if status["borrow_open"] else "当前尚未开放预算池间调拨"),
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
            "idle_note": (f"{idle.get('ticks')} 次调度中，{idle.get('idle_ticks')} "
                          "次所有流程都处于空闲状态。"),
            "stalled_lanes": stalled[:6],
        }

    # -- P17d: the four panels, and the ops backlog behind one of them ------
    #
    # Chem's §8.8, which the retrospective's 3.5 asks for: run state, source
    # gaps, pending failures and output acceptance shown *together*.  They
    # existed here already and were four pages apart, which is how a "health
    # OK" page came to sit beside a research gap nobody had filled.  Each tile
    # counts what its own detail page shows and links to it; none of them is a
    # second, independent reading that could disagree with the page it links to.

    def _failure_ledger_events(self) -> list[dict[str, Any]] | None:
        """Every park/resume event, or ``None`` when there is no ledger yet."""

        from .lane_failure_ledger import (
            LaneFailureLedger, LaneFailureLedgerError, default_path,
        )

        path = default_path(self.config.state_dir)
        if not path.is_file():
            return None
        try:
            with LaneFailureLedger(path, read_only=True) as ledger:
                return ledger.events()
        except (LaneFailureLedgerError, sqlite3.Error, OSError, ValueError):
            return None

    def ops_backlog(self) -> dict[str, Any]:
        """P17d 运维待办: what is parked, on which dependency, since when.

        Read-only and derived: the rows are a fold over the append-only lane
        failure ledger, so this page and a replay of the ledger cannot give
        different answers.  A Core whose lanes have never parked anything has
        no ledger file, and that is reported as "nothing has been parked"
        rather than as an error -- and, importantly, not as an empty table that
        looks like a working page with nothing in it.
        """

        from .lane_failure_ledger import summarise_events

        rows = self._failure_ledger_events()
        if rows is None:
            return {
                "available": False,
                "reason": "这台机器还没有任何流水线因为依赖不可用而挂起过工作",
                "dependencies": [], "parked_items": 0,
                "terminal_items": [], "terminal_count": 0,
                "permission_items": [], "permission_count": 0,
            }
        backlog = summarise_events(rows)
        members: dict[str, dict[str, Any]] = {}
        current_mission_version: str | None = None
        try:
            with self._core() as core:
                mission = self._mission(core)
                members = self._members(mission)
                mission_ref = mission.get("mission_ref")
                version = mission.get("version")
                if (isinstance(mission_ref, str)
                        and mission_ref.startswith("coverage-mission:")
                        and isinstance(version, int)):
                    current_mission_version = (
                        "coverage-mission-version:"
                        + mission_ref.removeprefix("coverage-mission:")
                        + f":{version}"
                    )
        except (CockpitMissionMissing, sqlite3.Error, ValueError, TypeError):
            pass
        latest_model_specs: dict[str, dict[str, Any]] = {}
        try:
            with self._core() as core:
                for row in core.execute(
                        "SELECT company_ref,state_hash,created_at FROM "
                        "coverage_mission_company_model_specs ORDER BY created_at DESC"):
                    latest_model_specs.setdefault(str(row["company_ref"]), {
                        "state_hash": str(row["state_hash"]),
                        "created_at": str(row["created_at"]),
                    })
        except (sqlite3.Error, ValueError, TypeError):
            pass
        latest_model_inputs: dict[str, dict[str, Any]] = {}
        model_attempts = [item for bucket in backlog["dependencies"]
                          for item in bucket["items"]]
        model_attempts.extend(backlog["terminal_items"])
        for item in model_attempts:
            if item.get("lane") != "mission_model_spec":
                continue
            key = item.get("item_key")
            seen_at = item.get("last_seen") or item.get("first_seen")
            if not isinstance(key, str) or not isinstance(seen_at, str):
                continue
            parts = key.split("|")
            if (len(parts) < 2 or not parts[0].startswith("company:")
                    or re.fullmatch(r"[0-9a-f]{64}", parts[1]) is None):
                continue
            prior = latest_model_inputs.get(parts[0])
            if prior is None or seen_at > prior["last_seen"]:
                latest_model_inputs[parts[0]] = {
                    "state_hash": parts[1], "last_seen": seen_at}
        governance = self._governance_records()
        permission_records = {
            "mission_catalyst_calendar": "yfinance-calendar-v1.json",
            "mission_consensus": "yfinance-analyst-estimates-v1.json",
            "mission_market_prices": "yfinance-daily-prices-v1.json",
        }

        def historical(item: Mapping[str, Any]) -> bool:
            return (_ops_superseded_mission(
                        item.get("item_key"), current_mission_version)
                    or _ops_superseded_model_spec(item, latest_model_specs, latest_model_inputs))

        dependencies = []
        historical_items = []
        for bucket in backlog["dependencies"]:
            active = [item for item in bucket["items"]
                      if not historical(item)]
            historical_items.extend({
                **item, "history_status": (
                    "mission_superseded" if _ops_superseded_mission(
                        item.get("item_key"), current_mission_version)
                    else "model_spec_superseded"),
                "history_note": (
                    "任务目标已更新，保留这次等待记录供追溯"
                    if _ops_superseded_mission(
                        item.get("item_key"), current_mission_version)
                    else ("后续公司模型定义已成功，保留这次失败记录供追溯"
                          if _ops_model_spec_history_reason(
                              item, latest_model_specs, latest_model_inputs)
                          == "later_success"
                          else "公司模型输入已经更新，这次旧输入失败仅保留供追溯")),
                "lane_label": REGISTRY_LANE_LABELS.get(item["lane"], item["lane"]),
                "item_label": _ops_item_label(item.get("item_key"), members),
                "technical_details": {"item_key": item.get("item_key"),
                                      "reason": item.get("reason"),
                                      "lane": item.get("lane")},
            } for item in bucket["items"] if item not in active)
            if not active:
                continue
            dependencies.append({
                **bucket,
                "item_count": len(active),
                "attempts": sum(int(item.get("attempts") or 0) for item in active),
                "first_seen": min(item["first_seen"] for item in active),
                "last_seen": max(item["last_seen"] for item in active),
                "lanes": sorted({item["lane"] for item in active}),
                "dependency_label": DEPENDENCY_LABELS.get(
                    bucket["dependency"], bucket["dependency"]),
                "lane_labels": [REGISTRY_LANE_LABELS.get(lane, lane)
                                for lane in sorted({item["lane"] for item in active})],
                "items": [
                    {**item,
                     "lane_label": REGISTRY_LANE_LABELS.get(item["lane"], item["lane"]),
                     "item_label": _ops_item_label(item.get("item_key"), members),
                     "display_reason": _ops_waiting_reason(item.get("reason")),
                     "technical_details": {
                         "item_key": item.get("item_key"),
                         "reason": item.get("reason"),
                         "lane": item.get("lane"),
                     }}
                    for item in active
                ],
            })
        terminal = [
            {**row,
             "lane_label": REGISTRY_LANE_LABELS.get(row["lane"], row["lane"]),
             "item_label": _ops_item_label(row.get("item_key"), members),
             "display_reason": _terminal_display_reason(
                 row.get("reason"), row.get("failure_class") or row.get("classification")),
             "technical_details": {
                 "item_key": row.get("item_key"),
                 "reason": row.get("reason"),
                 "lane": row.get("lane"),
             }}
            for row in backlog["terminal_items"]
        ]
        permissions = [
            {**row,
             "lane_label": REGISTRY_LANE_LABELS.get(row["lane"], row["lane"]),
             "item_label": _ops_item_label(row.get("item_key"), members),
             "display_reason": _ops_waiting_reason(row.get("reason"), permission=True),
             "technical_details": {
                 "item_key": row.get("item_key"),
                 "reason": row.get("reason"),
                 "lane": row.get("lane"),
             }}
            for row in backlog["permission_items"]
            if governance.get(permission_records.get(row.get("lane"), "")) != "approved"
        ]
        for row in backlog["permission_items"]:
            record = permission_records.get(row.get("lane"))
            if record is not None and governance.get(record) == "approved":
                historical_items.append({
                    **row, "history_status": "configuration_updated",
                    "history_note": "配置已更新，等待新运行确认",
                    "lane_label": REGISTRY_LANE_LABELS.get(row["lane"], row["lane"]),
                    "item_label": _ops_item_label(row.get("item_key"), members),
                    "technical_details": {"item_key": row.get("item_key"),
                                          "reason": row.get("reason"),
                                          "lane": row.get("lane")},
                })
        active_parked = sum(bucket["item_count"] for bucket in dependencies)
        return {
            "available": True, "as_of": _iso(self.clock()),
            "window": backlog["window"], "events": backlog["events"],
            "dependencies": dependencies,
            "parked_items": active_parked,
            "terminal_items": terminal,
            "terminal_count": backlog["terminal_count"],
            "permission_items": permissions,
            "permission_count": len(permissions),
            "historical_items": historical_items,
            "historical_count": len(historical_items),
            "class_labels": dict(FAILURE_CLASS_LABELS),
            "note": ("暂缓的任务会在相关服务恢复后自动重试；待授权的任务会在批准后继续。"
                     "已停止的任务保留具体原因，修复问题后再安排执行。"),
        }

    def _panel_lanes(self, lanes: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """Panel 1: the lane rows on this very page, counted by status."""

        counts = {key: 0 for key in LANE_STATUS_BUCKETS}
        other = 0
        for row in lanes:
            bucket = LANE_STATUS_BUCKET_OF.get(str(row.get("status") or ""))
            if bucket is None:
                other += 1
            else:
                counts[bucket] += 1
        stuck = counts["ungranted"] + counts["unapproved"] + counts["held"]
        return {
            "counts": counts, "labels": dict(LANE_STATUS_BUCKETS),
            "other": other, "total": len(lanes),
            "waiting_on_you": counts["ungranted"] + counts["unapproved"],
            "headline": stuck,
            "note": (f"{counts['running']} 项执行中，{counts['idle']} 项待命，"
                     f"{stuck} 项受阻"),
            "link": "lanes",
        }

    def _panel_gaps(self, core: sqlite3.Connection,
                    members: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
        """Panel 2: the source gaps -- framework questions plus unread documents."""

        framework_gaps = self._open_framework_gaps(core)
        backlog = self._extraction_backlog_total(core, members)
        parts = []
        if framework_gaps["available"]:
            parts.append(f"行业框架还缺 {framework_gaps['open']} 项")
        if backlog["available"]:
            parts.append(f"{backlog['queued_documents']} 份资料还没读")
        headline = (
            (framework_gaps.get("open") or 0) + (backlog.get("queued_documents") or 0)
        )
        return {
            "framework_gaps": framework_gaps, "extraction_backlog": backlog,
            "headline": headline,
            "note": "；".join(parts) or "这个 Core 还没有可数的缺口",
            "link": "sources",
        }

    def _open_framework_gaps(self, core: sqlite3.Connection) -> dict[str, Any]:
        """Open gaps on the newest framework version of each industry.

        Read straight off ``industry_framework_versions`` rather than through
        ``IndustryFrameworkAuthority``: that class takes a store and applies its
        schema, and this connection is ``mode=ro``.
        """

        if not _table_exists(core, "industry_framework_versions"):
            return {"available": False,
                    "reason": "industry_framework_versions 不在这个 Core 里"}
        newest: dict[str, tuple[int, str]] = {}
        for row in self._rows(core, (
            "SELECT industry_ref, version_number, record_json "
            "FROM industry_framework_versions"
        )):
            industry = str(row["industry_ref"])
            version = int(row["version_number"] or 0)
            if industry not in newest or version > newest[industry][0]:
                newest[industry] = (version, row["record_json"])
        by_industry: list[dict[str, Any]] = []
        total = 0
        for industry, (version, record_json) in sorted(newest.items()):
            try:
                gaps = json.loads(record_json).get("gaps") or []
            except (TypeError, ValueError):
                continue
            open_gaps = [gap for gap in gaps
                         if isinstance(gap, Mapping) and gap.get("status") != "covered"]
            total += len(open_gaps)
            by_industry.append({
                "industry_ref": industry, "version": version,
                "open": len(open_gaps), "gaps": len(gaps),
                "labels": [str(gap.get("label") or gap.get("gap_ref"))
                           for gap in open_gaps[:4]],
            })
        return {"available": True, "open": total, "industries": by_industry}

    def _extraction_backlog_total(
        self, core: sqlite3.Connection, members: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, Any]:
        """W2's per-company backlog reader, summed over the covered companies.

        The reader is reused rather than re-queried: its three queue kinds
        (open here, acquired under an older mission version, discovered but
        never fetched) are the distinction the W2 report exists to make, and a
        fresh ``COUNT(*)`` here would quietly lose it.
        """

        if not _table_exists(core, "coverage_mission_discovered_documents"):
            return {"available": False,
                    "reason": "这个 Core 还没有资料发现表"}
        from .extraction_backlog import (
            ExtractionBacklogError, extraction_backlog, observed_yield,
        )

        queued = awaiting = unqueued = discovered = 0
        counted = 0
        unavailable = {"available": False,
                       "reason": "研究目标里没有可数的公司"}
        if not members:
            return unavailable
        try:
            pointer = core.execute(
                "SELECT mission_version_id FROM coverage_mission_pointer "
                "ORDER BY mission_ref LIMIT 1"
            ).fetchone()
            mission_version_ref = None if pointer is None else pointer[0]
            yields = observed_yield(core)
        except (ExtractionBacklogError, sqlite3.Error, ValueError):
            return unavailable
        for company_ref in sorted(members):
            try:
                backlog = extraction_backlog(
                    core, company_ref,
                    mission_version_ref=mission_version_ref,
                    yields=yields,
                )
            except (ExtractionBacklogError, sqlite3.Error, ValueError):
                continue
            counted += 1
            queued += int(backlog["totals"]["queued_documents"])
            for tier in backlog["tiers"]:
                awaiting += int(tier["awaiting_extraction"])
                unqueued += int(tier["acquired_unqueued"])
                discovered += int(tier["discovered"])
        if counted == 0:
            return unavailable
        return {
            "available": True, "companies": counted,
            "queued_documents": queued,
            "awaiting_extraction": awaiting,
            "acquired_unqueued": unqueued,
            "discovered": discovered,
        }

    def _panel_failures(self) -> dict[str, Any]:
        """Count current recovery/authorization work; retain stopped attempts as history."""

        backlog = self.ops_backlog()
        if not backlog["available"]:
            return {
                "available": False, "headline": 0,
                "note": backlog["reason"], "link": "ops",
                "parked_items": 0, "terminal_count": 0, "dependencies": [],
            }
        top = [
            {"dependency": bucket["dependency"],
             "dependency_label": bucket["dependency_label"],
             "item_count": bucket["item_count"],
             "first_seen": bucket["first_seen"],
             "last_seen": bucket["last_seen"]}
            for bucket in backlog["dependencies"][:3]
        ]
        if top:
            note = "，".join(
                f"{row['dependency_label']}：{row['item_count']} 项等待处理" for row in top)
        elif backlog["permission_count"]:
            note = f"{backlog['permission_count']} 项等待授权"
        else:
            note = "目前没有待恢复或待授权的任务，已停止的尝试保留在历史记录中"
        return {
            "available": True,
            "headline": backlog["parked_items"] + backlog["permission_count"],
            "permission_count": backlog["permission_count"],
            "parked_items": backlog["parked_items"],
            "terminal_count": backlog["terminal_count"],
            "dependencies": top, "note": note, "link": "ops",
        }

    def _panel_acceptance(self, core: sqlite3.Connection) -> dict[str, Any]:
        """Panel 4: last week's output acceptance -- Q1's scores, Q2's window.

        The window is Q2's own ``closed_week``, not "the last seven days": the
        weekly reflection reports on the week that ended, and a tile computing
        a different week would put two numbers about "last week" on one page.
        """

        from .research_cycle_reflection import (
            closed_week, journal_feedback, quality_scores_published,
        )

        window = closed_week(self.clock())
        scores = quality_scores_published(core, window)
        feedback = journal_feedback(core, window)
        if not scores.get("available"):
            return {
                "available": False, "headline": 0, "week": window["iso_week"],
                "note": scores.get("reason"), "link": "reflection",
            }
        published = int(scores.get("published") or 0)
        judged = int(scores.get("judged") or 0)
        deterministic = int(scores.get("deterministic_only") or 0)
        entries = int(feedback.get("entries") or 0) if feedback.get("available") else 0
        if published == 0:
            note = "上周没有研究产出完成质量评估"
        else:
            note = (f"上周完成 {published} 份质量评估，覆盖 "
                    f"{scores.get('distinct_targets')} 份产出")
            if entries:
                note += f"；你留下 {entries} 条反馈"
            elif feedback.get("available"):
                note += "；你还没留反馈"
        return {
            "available": True, "headline": published, "week": window["iso_week"],
            "window": {"start": window["start"], "end": window["end"]},
            "published": published, "judged": judged,
            "deterministic_only": deterministic,
            "distinct_targets": scores.get("distinct_targets"),
            "by_rubric": scores.get("by_rubric"),
            "feedback": (feedback if feedback.get("available")
                         else {"available": False, "reason": feedback.get("reason")}),
            "note": note, "link": "reflection",
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

        cache_key = (str(spec.driver_key or ""), str(spec.operation))
        if cache_key in self._lane_governance_cache:
            return self._lane_governance_cache[cache_key]
        if spec.argv_fragment is None:
            self._lane_governance_cache[cache_key] = None
            return None
        try:
            argv = spec.argv_fragment(context)
        except Exception:  # noqa: BLE001 - a lane's fragment is not the page's problem
            return None
        for value in argv:
            if isinstance(value, str) and "connector-governance" in value:
                result = Path(value).name
                self._lane_governance_cache[cache_key] = result
                return result
        self._lane_governance_cache[cache_key] = None
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
            if status.startswith("unavailable:"):
                raw_status = status
                status = "unavailable"
                detail = (f"{detail}；原始状态：{raw_status}"
                          if detail else f"原始状态：{raw_status}")
            note = LANE_STATUS_NOTES.get(status)
            record = self._lane_governance_record(spec, context)
            if record is not None and record in governance and governance[record] != "approved":
                # The record is on disk and the owner has not approved it --
                # or it is on disk and unreadable, which is not approval
                # either. The lane will keep starting children that refuse, so
                # the honest word is not "idle" and not "failed": it is
                # "waiting for you".
                status = "unapproved"
                note = "数据源配置已安装，等待审批"
                detail = f"{detail}；governance_record={record}" if detail else f"governance_record={record}"
            if note is None:
                # A status this panel has no sentence for. Shown rather than
                # hidden, because a lane nobody can read about is the thing
                # this panel exists to stop -- but it is a gap here, not a
                # lane's fault, and the raw word is all there is to show.
                note = "出现尚未识别的运行状态，原始状态见技术详情"
            skipped = result.get("skipped")
            if isinstance(skipped, list) and skipped:
                reasons = [str(item.get("reason")) for item in skipped
                           if isinstance(item, Mapping) and item.get("reason")]
                if reasons:
                    joined = "跳过原因：" + "；".join(sorted(set(reasons))[:3])
                    detail = f"{detail}；{joined}" if detail else joined
            if key == "guidepoint_discovery" and (
                detail == "all_grants_refused；跳过原因：CoverageMissionConflict: mission marks source:guidepoint as not_connected"
                or detail == "all_grants_refused；跳过原因：CoverageMissionConflict——任务配置将 source:guidepoint 标记为 尚未连接。"
            ):
                # The connector can be healthy while this mission grants it no
                # source authority.  Calling that state idle hides the action
                # boundary from the owner; retain the driver's exact reason as
                # detail and project only its known meaning here.
                status = "ungranted"
                note = "当前研究任务尚未启用专家访谈资料来源"
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
        # Exit zero proves only that the child wrote its summary.  Product
        # refusal/failure remains a failure on the activity page.
        product_statuses = (
            summary.get(key) for key in (
                "map_status", "judgement_status", "dossier_status",
                "memo_status", "framework_status", "gate_status",
                "deliverable_status", "forecast_status", "sensitivity_status",
            )
        )
        failures = {
            "failed", "refused", "unverified", "verifier_rejected",
            "model_unavailable", "not_independent", "rejected", "gated",
            "gated:same_family", "verification_failed", "unresolvable_refs",
            "rubric_refused", "constitution_refused", "binding_drift",
        }
        # The process-level status says only whether the child completed.  The
        # lane-specific field is the authority outcome, and several producers
        # deliberately exit zero after recording a refusal.
        if state == "done" and (
            summary.get("status") in failures or any(
                isinstance(value, str) and (
                    value in failures or value.startswith("refused:") or
                    value.endswith("_refused") or value.endswith("_failed")
                ) for value in product_statuses
            )
        ):
            state = "failed"
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

    def _journal_event_view(self, row: Mapping[str, Any]) -> dict[str, Any]:
        """Project legacy journal rows without changing their stored evidence."""
        title, detail = str(row["title"]), row["detail"]
        try:
            refs = json.loads(row["refs_json"] or "{}")
        except (TypeError, ValueError):
            refs = {}
        technical = None
        if row["kind"] == "approval" and isinstance(refs, Mapping):
            decision = refs.get("decision")
            titles = {
                "admit": "接受了研究论点", "approve": "批准了研究决定",
                "reject": "未批准研究决定", "retired": "停止使用了一条结论",
                "kept": "保留了一条被标记的结论",
                "return_for_more_work": "退回研究内容以补充资料",
                "keep_forecast": "维持了预测", "revise_forecast": "决定修订预测",
                "accept": "接受了论点修订", "defer": "暂缓决定论点修订",
                "decline": "不同意重新评估研究报告",
                "publish": "发布了研究目标", "discard": "放弃了研究草稿",
            }
            if decision in titles:
                technical = {"original_title": title, "refs": refs}
                title = titles[decision]
        elif row["kind"] == "model_budget" and isinstance(refs, Mapping):
            from .model_selection import PURPOSE_LABELS
            purpose = refs.get("purpose")
            if isinstance(purpose, str):
                title = f"已调整「{PURPOSE_LABELS.get(purpose, '研究模型')}」的调用预算"
                technical = {"original_title": row["title"], "purpose": purpose,
                             "revision": refs.get("revision"), "original_detail": detail}
                detail = "新预算已保存"
        return {"title": title, "detail": detail, "technical": technical}

    @staticmethod
    def _deliverable_log_summary(value: Any) -> tuple[str, dict[str, Any] | None]:
        """Display a system-authored deliverable summary without changing its bytes."""
        raw = str(value or "")
        from decimal import Decimal, ROUND_HALF_UP
        from .numeric_display import format_display_number, transform_unquoted_prose
        from .research_gap_display import display_metadata_text

        def summary_values(part: str) -> str:
            part = re.sub(
                r"(?i)\bUSD\s+([+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)"
                r"(?![A-Za-z0-9.,])",
                lambda match: (f"{(Decimal(match.group(1).replace(',', '')) / Decimal('100000000')).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)} 亿美元"
                               if abs(Decimal(match.group(1).replace(',', ''))) >= Decimal('100000000')
                               else match.group(0)),
                part,
            )
            part = part.replace("AI-native", "AI 原生")
            return re.sub(
                r"(?<![A-Za-z0-9.])([+-]?\d+(?:\.\d+)?)%(?![A-Za-z0-9.])",
                lambda match: format_display_number(match.group(1), kind="percent"), part)

        source_display = raw[:200]
        shown = display_metadata_text(transform_unquoted_prose(source_display, summary_values))
        return shown, ({"original_summary": raw} if shown != raw else None)

    def log(self, *, since: str | None = None, limit: int = 150) -> dict[str, Any]:
        limit = max(1, min(int(limit), 500))
        with self._core() as core:
            try:
                mission = self._mission(core)
            except CockpitMissionMissing:
                return {"as_of": _iso(self.clock()), "events": [], "service_state": "awaiting_mission", "last_tick_at": None}
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
            summary, summary_technical = self._deliverable_log_summary(record["summary"])
            events.append({
                "id": f"deliverable:{record['id']}", "at": record["created_at"],
                "kind": "deliverable", "lane": "写文档",
                "title": f"写好了初步筛选第 {record['version']} 版（{written}/{len(record['sections'])} 节）",
                "detail": summary, "technical": summary_technical, "state": "done",
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
            shown = self._journal_event_view(row)
            events.append({"id": f"cockpit:{row['event_id']}", "at": row["at"], "kind": row["kind"], "lane": "你",
                           "title": shown["title"], "detail": shown["detail"],
                           "technical": shown["technical"], "state": "done", "company": None})
        heartbeat = _load_json(self.config.heartbeat_path) or {}
        for key, label in (("bounded_planner", "研究调度"), ("outbox", "消息投递"), ("weekly_brief", "每周简报"), ("backup", "备份")):
            lane = heartbeat.get(key) or {}
            if lane.get("last_error"):
                raw_error = str(lane["last_error"])[:400]
                events.append({"id": f"error:{key}:{lane.get('last_completed_at')}", "at": lane.get("last_completed_at") or heartbeat.get("last_tick_at"),
                               "kind": "problem", "lane": label, "title": f"{label}遇到问题",
                               "detail": _runtime_error_display(raw_error),
                               "technical": {"original_error": raw_error},
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
            try:
                mission = self._mission(core)
            except CockpitMissionMissing:
                return {"as_of": _iso(self.clock()), "items": [], "count": 0}
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
            # P12d: the Deep Insight Gate's twelve answers, waiting for you.
            # It is the Playbook's own human checkpoint between a first screen
            # and full coverage, and automation never passes it: approving is
            # what writes the ``gate_passed`` stage record and lets the company
            # go on. The answers travel with the item because a gate you have to
            # open another view to read is a gate you decide without reading.
            #
            # Whether it gets buttons is read off the Core, the same way the
            # revision checkpoints above do it: the mission stage ledger is
            # scoped by version and the live mission rolls, so a draft can stop
            # being decidable without anybody touching it. It still appears --
            # it is still what the owner has to deal with -- and it says why,
            # because a button that goes nowhere is worse than an item that
            # says so.
            for row in self._rows(core,
                "SELECT v.* FROM deep_insight_gate_versions v "
                "LEFT JOIN deep_insight_gate_decisions d "
                "ON d.gate_version_ref=v.version_id "
                "WHERE d.decision_id IS NULL ORDER BY v.created_at",
            ):
                record = json.loads(row["record_json"])
                verdict = _gate_decidability(core, record)
                answered = [item for item in record["answers"]
                            if item["status"] == "answered"]
                items.append({
                    "kind": "deep_insight_gate", "ref": row["version_id"],
                    "hash": row["content_hash"], "at": row["created_at"],
                    "title": "深度认知评审十二问：是否让这家公司进入完整覆盖",
                    "who": self._label(members, row["company_ref"]),
                    "summary": (f"第 {row['version_number']} 版；十二问答了 "
                                f"{len(answered)} 问，其余写明缺什么、下一步取什么。"),
                    # One string per question, keyed by its number. The details
                    # renderer joins an array with 、 and stringifies an object,
                    # so a list of twelve answer objects would arrive as twelve
                    # "[object Object]" run together -- the page has one shape
                    # for a value and it is a line of text.
                    "details": {
                        "行业分类": record["classification"],
                        **{item["question_ref"]: _gate_answer_line(item)
                           for item in record["answers"]},
                        "档案版本": record["bindings"]["dossier_version_ref"],
                        "争议图版本": record["bindings"]["debate_map_version_ref"],
                    },
                    "detail_labels": {
                        "行业分类": "行业分类",
                        **{item["question_ref"]: f"研究问题 {number}"
                           for number, item in enumerate(record["answers"], 1)},
                    },
                    "actions": list(GATE_ACTIONS) if verdict["decidable"] else [],
                    "needs_rationale": verdict["decidable"],
                    **({} if verdict["decidable"]
                       else {"note": "暂时不能裁决：" + verdict["reason"]}),
                })
            # Investment Memo reuses MissionDeliverable and the mission stage
            # ledger. Only the active mission's current head is offered; an old
            # version remains readable under documents but cannot receive a
            # verdict meant for different bytes.
            try:
                memo_rows = self._rows(core,
                    "SELECT v.* FROM mission_deliverable_pointer p "
                    "JOIN mission_deliverable_versions v ON v.version_id=p.version_id "
                    "WHERE v.kind='investment_memo' AND v.mission_version_ref=? "
                    "ORDER BY v.created_at", (mission["id"],))
            except sqlite3.OperationalError:
                memo_rows = []
            for row in memo_rows:
                record = json.loads(row["record_json"])
                histories = self._rows(core,
                    "SELECT stage_ref,status,r.record_json FROM coverage_mission_stage_records r "
                    "JOIN coverage_mission_versions v ON v.mission_version_id=r.mission_version_ref "
                    "WHERE v.mission_ref=? AND r.company_ref=? ORDER BY r.created_at,r.record_id",
                    (mission["mission_ref"], record["subject_ref"]))
                decided = any(item["stage_ref"] == "investment_memo"
                              and item["status"] in {"gate_passed", "gate_failed"}
                              and record["id"] in (json.loads(item["record_json"]).get("evidence_refs") or [])
                              for item in histories)
                if decided:
                    continue
                note = None
                actions = [{"decision": "approve", "label": "批准并进入持续覆盖"},
                           {"decision": "reject", "label": "拒绝"}]
                try:
                    from .investment_memo_contract import validate_memo_gate, verified_body_hash
                    playbook_rows = self._rows(core,
                        "SELECT record_json,content_hash FROM research_playbook_versions "
                        "WHERE playbook_version_id=?", (record["playbook_version_ref"],))
                    playbook_row = playbook_rows[0] if playbook_rows else None
                    if playbook_row is None or playbook_row["content_hash"] != record.get("playbook_version_hash"):
                        raise ValueError("bound Playbook is missing or changed")
                    playbook = json.loads(playbook_row["record_json"])
                    questions = [{"question_ref": f"memo_q{index:02d}", "question": question}
                                 for index, question in enumerate(playbook.get("key_questions") or [], 1)]
                    validate_memo_gate(record.get("gate") or {},
                                       material_hash=verified_body_hash(record),
                                       expected_questions=questions)
                    router_path = self._model_router_db()
                    if router_path is None:
                        raise ValueError("model router authority is unavailable")
                    from .investment_memo_evidence import replay_memo_model_evidence
                    model_evidence = replay_memo_model_evidence(
                        scheduler_db=self.config.scheduler_db,
                        model_router_db=Path(router_path), gate=record["gate"],
                        mission=mission,
                    )
                    if model_evidence["status"] != "verified":
                        raise ValueError(model_evidence.get("reason") or "formal model evidence is unverified")
                except (ValueError, KeyError, TypeError, sqlite3.OperationalError) as exc:
                    actions = []
                    note = f"暂时无法提交决定：投资备忘录的核验信息不完整。具体原因见技术详情。"
                    model_evidence = {"status": "unverified", "reason": str(exc),
                                      "producer_calls": [], "verifier": None}
                gate = record.get("gate") or {}
                details = {section["title"]: section.get("body") or "（本节为空）"
                           for section in record.get("sections") or []}
                details.update({q["question_ref"]: q.get("answer") or "（未回答）"
                                for q in gate.get("key_questions") or []})
                details["独立核验"] = (gate.get("verifier") or {}).get("verdict") or "缺失"
                items.append({
                    "kind": "investment_memo", "ref": record["id"],
                    "hash": row["content_hash"], "at": row["created_at"],
                    "title": "投资备忘录：是否批准进入持续覆盖",
                    "who": self._label(members, record["subject_ref"]),
                    "summary": record.get("summary") or "",
                    "details": details, "actions": actions,
                    "memo_review": {
                        "producer_groups": [dict(row) for row in gate.get("producer_calls") or []],
                        "key_questions": [dict(row) for row in gate.get("key_questions") or []],
                        "checks": [dict(row) for row in gate.get("checks") or []],
                        "gaps": list(record.get("gaps") or []),
                        "input_bindings": [dict(row) for row in gate.get("input_bindings") or []],
                        "verified_body_hash": gate.get("verified_body_hash"),
                        "formal_evidence": model_evidence,
                        "human_signature_required": True,
                    },
                    "needs_rationale": bool(actions),
                    **({} if note is None else {"note": note}),
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
                        "审批状态": "等待正式研究审批流程接入；本页暂不能提交决定",
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
        # P14-M2: a model a calling stage was using has gone from OpenClaw and
        # the stage fell to its next link on its own. Nothing is broken, which
        # is exactly why it belongs here: the work carries on under a model the
        # owner did not choose, and the only thing that would ever surface that
        # is a notice that stays until somebody reads it.
        items.extend(self._model_fallback_items())
        for item in items:
            labels = APPROVAL_DETAIL_LABELS.get(item["kind"], {})
            if labels:
                item["detail_labels"] = {
                    **item.get("detail_labels", {}),
                    **{key: label for key, label in labels.items()
                       if key in (item.get("details") or {})},
                }
        items.sort(key=lambda i: i["at"])
        return {"schema_version": SCHEMA_VERSION, "as_of": _iso(self.clock()), "items": items, "count": len(items)}

    def workspaces(self, login: str) -> dict[str, Any]:
        from .workspace import WorkspaceError
        from .workspace_manager import list_workspaces
        try:
            return list_workspaces(getattr(self.config, "workspace_manager_config_path", None),
                                   login, self.workspace_context.get("workspace_id"))
        except (WorkspaceError, OSError, ValueError) as exc:
            raise CockpitError("研究环境列表暂时不可用") from exc

    def create_workspace(self, login: str, value: Mapping[str, Any]) -> dict[str, Any]:
        from .workspace import WorkspaceError
        from .workspace_manager import request_create
        import subprocess
        path = getattr(self.config, "workspace_manager_config_path", None)
        if path is None:
            raise CockpitError("研究环境管理尚未配置")
        try:
            return request_create(path, login, value)
        except WorkspaceError as exc:
            raise CockpitError(str(exc)) from exc
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            raise CockpitError("研究环境尚未准备完成，可以重试同一次创建请求") from exc

    def _model_router_db(self) -> str | None:
        """The model catalog this Core reads, named by its model configuration."""

        if self.config.model_config_path is None:
            return None
        config = _load_json(self.config.model_config_path)
        path = config.get("model_router_db") if isinstance(config, dict) else None
        if not isinstance(path, str) or not Path(path).is_file():
            return None
        return path

    def _model_fallback_items(self) -> list[dict[str, Any]]:
        """Open "the model you were using has gone" notices, as approval rows.

        Read-only and best effort: a Core with no model catalog has no notices,
        and an unreadable one must not be able to empty the approvals page.
        """

        path = self._model_router_db()
        if path is None:
            return []
        from .model_router import ModelRouter

        try:
            with closing(ModelRouter(path, read_only=True)) as router:
                notices = router.fallback_notices(open_only=True)
        except (sqlite3.Error, OSError, ValueError):
            return []
        items: list[dict[str, Any]] = []
        for notice in notices:
            replacement = notice["replacement_profile_id"]
            items.append({
                "kind": "model_fallback", "ref": notice["id"],
                "hash": notice["content_hash"], "at": notice["created_at"],
                "title": "有个环节换模型了：原来用的那个在 OpenClaw 上没有了",
                "who": "模型",
                "summary": notice["message"],
                "details": {
                    "消失的模型": notice["profile_id"],
                    "环节": notice["purpose"],
                    "现在用": replacement or "没有可用的模型",
                    "档位": MODEL_TIER_LABELS.get(notice["tier"], notice["tier"]),
                },
                # 「知道了」 changes no routing -- the fall already happened -- so
                # it needs no reason. Re-selecting is the other way to clear it,
                # and that is a different page and a different decision.
                "actions": [{"decision": "acknowledge", "label": "知道了"}],
                "needs_rationale": False,
            })
        return items

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
            checkpoint_tables = list(CHECKPOINT_TABLES.items())
            checkpoint_tables.append(("thesis_revision_candidate", (
                "zero_base_revision_candidates", "candidate_id",
                "thesis_revision_decisions", "candidate_ref")))
            for kind, (table, key, decisions, ref_column) in checkpoint_tables:
                if not _table_exists(core, table):
                    continue
                decidable = _table_exists(core, decisions)
                sql = f"SELECT t.* FROM {table} t "
                if decidable and kind != "gate_reopen":
                    join = f"LEFT JOIN {decisions} d ON d.{ref_column}=t.{key} "
                    # ``defer`` is a decision that does not close the
                    # candidate, so on the full ledger only a terminal row
                    # takes it off the page. A ledger without the column is
                    # the older shape, where any row means decided.
                    if _column_exists(core, decisions, "terminal"):
                        join += "AND d.terminal=1 "
                    sql += join + "WHERE d.rowid IS NULL "
                rows = self._rows(core, sql + "ORDER BY t.created_at,t." + key)
                superseded_counts: dict[str, int] = {}
                if kind == "gate_reopen":
                    # A newer assessment of the same company's same passed
                    # gate replaces the older pending card.  The append-only
                    # proposals remain in Core for audit; distinct companies,
                    # stages and passed versions remain separate decisions.
                    latest: dict[tuple[str, str, str], sqlite3.Row] = {}
                    ungrouped: list[sqlite3.Row] = []
                    counts: dict[tuple[str, str, str], int] = {}
                    for candidate_row in rows:
                        candidate_record = json.loads(candidate_row["record_json"])
                        group = (
                            str(candidate_record.get("company_ref") or ""),
                            str(candidate_record.get("stage_ref") or ""),
                            str(candidate_record.get("passed_version_ref") or ""),
                        )
                        if not all(group):
                            ungrouped.append(candidate_row)
                            continue
                        latest[group] = candidate_row
                        counts[group] = counts.get(group, 0) + 1
                    rows = sorted(
                        [*ungrouped, *latest.values()],
                        key=lambda candidate_row: (
                            candidate_row["created_at"], candidate_row[key]),
                    )
                    superseded_counts = {
                        latest[group][key]: count - 1
                        for group, count in counts.items() if count > 1
                    }
                    if decidable:
                        terminal_sql = f"SELECT {ref_column} FROM {decisions}"
                        if _column_exists(core, decisions, "terminal"):
                            terminal_sql += " WHERE terminal=1"
                        terminal_refs = {
                            decision_row[ref_column]
                            for decision_row in self._rows(core, terminal_sql)
                        }
                        rows = [candidate_row for candidate_row in rows
                                if candidate_row[key] not in terminal_refs]
                for row in rows:
                    record = json.loads(row["record_json"])
                    zero_base = None
                    if table == "zero_base_revision_candidates" and _table_exists(core, "zero_base_review_versions"):
                        review_row = core.execute(
                            "SELECT record_json FROM zero_base_review_versions WHERE version_id=?",
                            (record.get("review_version_ref"),),
                        ).fetchone()
                        if review_row is not None:
                            zero_base = json.loads(review_row["record_json"]).get("narrative")
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
                    actions = list(CHECKPOINT_ACTIONS[kind]) if decidable else []
                    stale_reason = None
                    if kind == "thesis_revision_candidate":
                        # A candidate is written against one immutable thesis
                        # version.  Accepting one the thesis has moved past is
                        # refused by the authority (correctly -- the old
                        # evidence must not overwrite a newer judgement), so
                        # the honest card says so and offers to close it, and
                        # the zero-base lane writes a fresh candidate against
                        # the current thesis on its next pass.
                        thesis_ref = record.get("thesis_ref")
                        pinned = record.get("thesis_version_ref")
                        if isinstance(thesis_ref, str) and isinstance(pinned, str):
                            current = core.execute(
                                "SELECT v.version_id FROM current_pointers p "
                                "JOIN thesis_versions v ON v.version_id=p.version_id "
                                "WHERE p.thesis_id=?", (thesis_ref,),
                            ).fetchone()
                            if current is not None and current["version_id"] != pinned:
                                stale_reason = (
                                    "这条建议针对的是旧版本的投资论点（论点此后已更新），"
                                    "接受会被系统拒绝。可关闭它；从零复盘下一轮会基于"
                                    "当前论点生成新建议。"
                                )
                                actions = [{"decision": "reject",
                                            "label": "关闭这条过期建议"}] if decidable else []
                    items.append({
                        "kind": kind, "ref": row[key], "hash": row["content_hash"],
                        "at": row["created_at"],
                        "title": ("从零复盘后，提议改我们对这家公司的判断"
                                  if table == "zero_base_revision_candidates"
                                  else CHECKPOINT_TITLES.get(kind) or kind),
                        "zero_base_review": zero_base,
                        "who": self._label(members, record.get("company_ref")),
                        "summary": summary,
                        "details": {name: value for name, value in details.items()
                                    if value not in (None, [], "")},
                        "display_reason": stale_reason,
                        # Both halves, side by side.
                        "reflection": reflections["by_judgement"].get(
                            record.get("judgement_ref")),
                        "actions": actions,
                        "needs_rationale": False if stale_reason else decidable,
                        **({"occurrence_count": superseded_counts[row[key]] + 1}
                           if row[key] in superseded_counts else {}),
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
        supplied_rationale = rationale.strip()
        # The governance operations historically require a non-empty audit
        # reason.  Keep that invariant without attributing words to the owner
        # when the optional Cockpit field is left blank.
        recorded_rationale = supplied_rationale or "系统记录：用户未填写补充说明"
        request_id = _text(value.get("request_id"), "request_id", maximum=128)
        actor = _subject_for_login(login)
        if kind.startswith("draft:"):
            return self._decide_draft(login, kind.split(":", 1)[1], ref, digest, decision, request_id)
        # P14-M2: 「知道了」 on a model that vanished from the gateway. It is on
        # this page because it is a thing waiting for the owner, and it takes
        # this path rather than its own because the page has one shape for
        # "something is waiting" and adding a second would be a second page.
        if kind == "model_fallback":
            if decision != "acknowledge":
                raise CockpitError("这条提示只能「知道了」")
            return self.acknowledge_model_notice(login, {"ref": ref})
        if kind == "thesis":
            if decision not in {"admit", "reject"}:
                raise CockpitError("decision must be admit or reject")
            operation, params = "decide_thesis_admission", {
                "candidate_id": ref, "candidate_hash": digest, "verdict": decision, "rationale": recorded_rationale,
                "decision_id": f"thesis-admission-decision:cockpit:{content_hash({'candidate': ref, 'request': request_id})[:24]}"}
            title = ("接受了研究论点" if decision == "admit" else "拒绝了研究论点")
        elif kind == "capability":
            if decision not in {"approve", "reject"}:
                raise CockpitError("decision must be approve or reject")
            evaluation = value.get("evaluation_id")
            operation, params = "decide_capability_promotion", {
                "proposal_ref": ref, "decision": decision, "rationale": recorded_rationale,
                "decision_id": f"capability-decision:cockpit:{content_hash({'proposal': ref, 'request': request_id})[:24]}",
                **({"evaluation_id": evaluation} if isinstance(evaluation, str) and evaluation else {})}
            title = ("批准了新工具" if decision == "approve" else "拒绝了新工具")
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
                "rationale": recorded_rationale}
            title = ("退役了一条结论" if decision == "retired" else "保留了一条被标记的结论")
        elif kind == "deep_insight_gate":
            if decision not in {"approve", "return_for_more_work", "reject"}:
                raise CockpitError(
                    "decision must be approve, return_for_more_work or reject")
            operation, params = "decide_deep_insight_gate", {
                "gate_version_ref": ref, "gate_version_hash": digest,
                "decision": decision, "reason": recorded_rationale}
            title = {"approve": "通过了深度认知评审", "return_for_more_work": "将深度认知评审退回补充",
                     "reject": "未通过深度认知评审"}[decision]
        elif kind == "investment_memo":
            if decision not in {"approve", "reject"}:
                raise CockpitError("decision must be approve or reject")
            operation, params = "decide_investment_memo", {
                "memo_version_ref": ref, "memo_version_hash": digest,
                "decision": decision, "reason": recorded_rationale}
            title = ("批准了投资备忘录" if decision == "approve"
                     else "未批准投资备忘录")
        elif kind == "forecast":
            if decision not in {"keep_forecast", "revise_forecast"}:
                raise CockpitError("decision must be keep_forecast or revise_forecast")
            operation, params = "decide_forecast_overturn", {
                "reconciliation_ref": ref, "reconciliation_hash": digest, "decision": decision, "rationale": recorded_rationale,
                "idempotency_key": f"cockpit-overturn:{ref}:{request_id}"}
            title = ("维持了预测" if decision == "keep_forecast" else "决定修订预测")
        elif kind == "thesis_revision_candidate":
            # ADR-0007: automation may never take this branch. The cockpit
            # mints an ephemeral *human* principal for the call, and the
            # writer refuses the operation for anything else.
            if decision not in {"accept", "reject", "defer"}:
                raise CockpitError("decision must be accept, reject or defer")
            operation, params = "decide_thesis_revision_candidate", {
                "candidate_ref": ref, "candidate_hash": digest,
                "verdict": decision, "reason": recorded_rationale}
            title = {"accept": "接受了论点修订", "reject": "未接受论点修订",
                     "defer": "暂缓决定论点修订"}[decision]
        elif kind == "gate_reopen":
            if decision not in {"approve", "decline"}:
                raise CockpitError("decision must be approve or decline")
            operation, params = "decide_gate_reopen", {
                "proposal_ref": ref, "proposal_hash": digest,
                "verdict": decision, "reason": recorded_rationale}
            title = ("同意重新出具初步筛查报告" if decision == "approve"
                     else "不同意重新出具初步筛查报告")
        else:
            raise CockpitError("unknown approval kind")
        try:
            result = self.governance_call(self.token_config, self.writer_socket, actor_ref=actor, operation=operation, params=params)
        except (GovernanceCliError, RemoteError) as exc:
            raise CockpitConflict(f"这项决定没有被接受：{_reason(exc)}") from exc
        self.journal.record_event(kind="approval", title=title,
                                  detail=supplied_rationale or None, login=login,
                                  refs={"kind": kind, "ref": ref,
                                        "decision": decision, "operation": operation,
                                        "rationale_provided": bool(supplied_rationale)})
        return {"status": "decided", "kind": kind, "ref": ref, "decision": decision,
                "result": result if isinstance(result, (dict, list)) else None}

    def decision_status(self, query: Mapping[str, Any]) -> dict[str, Any]:
        """Confirm one exact human decision after a lost HTTP response."""
        kind = _text(query.get("kind"), "kind", maximum=64)
        ref = _text(query.get("ref"), "ref", maximum=512)
        digest = _sha(query.get("hash"), "hash")
        decision = _text(query.get("decision"), "decision", maximum=32)
        if kind != "thesis_revision_candidate" or decision not in {
            "accept", "reject", "defer",
        }:
            return {"status": "not_confirmed", "decided": False}
        try:
            with self._core() as core:
                row = core.execute(
                    "SELECT decision_id,candidate_hash,verdict,terminal,"
                    "resulting_thesis_version_ref,content_hash "
                    "FROM thesis_revision_decisions WHERE candidate_ref=?", (ref,),
                ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc):
                raise
            row = None
        if not row:
            return {"status": "not_confirmed", "decided": False}
        exact = (row["candidate_hash"] == digest
                 and row["verdict"] == decision
                 and int(row["terminal"] or 0) == 1)
        if not exact:
            return {"status": "different_decision", "decided": False}
        return {
            "status": "decided", "decided": True, "kind": kind, "ref": ref,
            "decision": decision, "decision_ref": row["decision_id"],
            "decision_hash": row["content_hash"],
            "resulting_version_ref": row["resulting_thesis_version_ref"],
        }

    # -- claims, models and feedback -----------------------------------------

    def claims(self, *, company_ref: str | None = None, index_aspect: str | None = None,
               importance: str | None = None, canonical_only: bool = True,
               limit: int = MAX_CLAIMS_IN_VIEW, cursor: str | None = None) -> dict[str, Any]:
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
            try:
                mission = self._mission(core)
            except CockpitMissionMissing:
                return {"as_of": _iso(self.clock()), "indexed": True, "total": 0,
                        "returned_count": 0, "next_cursor": None, "items": [], "companies": [],
                        "filters": {"company_ref": company_ref, "aspect": index_aspect,
                                    "importance": importance, "canonical_only": canonical_only},
                        "vocabulary": {"aspects": [], "importance": []}}
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
                    "这个 Core 还没有构建研究论点与证据索引库，按主题或来源筛选在这里答不了"
                    if not indexed else f"筛选条件不对：{exc}"
                ) from exc
        # Most important first, then newest: a company report outranks a news
        # item about it, and among equals the recent one is the one to read.
        annotated.sort(key=lambda row: (row["index_order"], row["created_at"], row["ref"]))
        fingerprint = hashlib.sha256(json.dumps({
            "company_ref": company_ref, "aspect": index_aspect,
            "importance": importance, "canonical_only": canonical_only,
        }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        remaining = annotated
        if cursor:
            try:
                if not isinstance(cursor, str) or len(cursor) > 4096:
                    raise ValueError("cursor is not bounded")
                payload = json.loads(base64.urlsafe_b64decode(
                    cursor + "=" * (-len(cursor) % 4)))
                if (not isinstance(payload, dict)
                        or payload.get("filters") != fingerprint
                        or not isinstance(payload.get("after"), list)
                        or len(payload["after"]) != 3
                        or not all(isinstance(value, str) and len(value) <= 1024
                                   for value in payload["after"][1:])):
                    raise ValueError("cursor binding changed")
                raw_order = payload["after"][0]
                if isinstance(raw_order, bool):
                    raise ValueError("cursor order is invalid")
                if isinstance(raw_order, int):
                    index_order: Any = raw_order
                elif (isinstance(raw_order, list) and len(raw_order) == 4
                      and isinstance(raw_order[0], int)
                      and not isinstance(raw_order[0], bool)
                      and isinstance(raw_order[1], list) and len(raw_order[1]) == 2
                      and isinstance(raw_order[1][0], int)
                      and not isinstance(raw_order[1][0], bool)
                      and raw_order[1][0] in {0, 1}
                      and all(isinstance(value, str) and len(value) <= 1024
                              for value in (raw_order[1][1], raw_order[2], raw_order[3]))):
                    index_order = (raw_order[0], (raw_order[1][0], raw_order[1][1]),
                                   raw_order[2], raw_order[3])
                else:
                    raise ValueError("cursor order is invalid")
                after = (index_order, payload["after"][1], payload["after"][2])
            except (ValueError, KeyError, TypeError, UnicodeDecodeError,
                    binascii.Error, json.JSONDecodeError) as exc:
                raise CockpitError("分页位置无效或筛选条件已经改变") from exc
            remaining = [row for row in annotated if (
                row["index_order"], row["created_at"], row["ref"]) > after]
        page_limit = max(1, min(int(limit), MAX_CLAIMS_IN_VIEW))
        selected = remaining[:page_limit]
        items = [{
            "ref": row["ref"], "statement": row["statement"],
            "company": self._label(members, row["subject_ref"]),
            "company_ref": row["subject_ref"], "period": row["period"],
            "period_label": _claim_period_label(row["period"]),
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
        } for row in selected]
        next_cursor = None
        if len(remaining) > len(selected):
            last = selected[-1]
            next_cursor = base64.urlsafe_b64encode(json.dumps({
                "filters": fingerprint,
                "after": [last["index_order"], last["created_at"], last["ref"]],
            }, sort_keys=True, separators=(",", ":")).encode()).decode().rstrip("=")
        return {
            "as_of": _iso(self.clock()), "indexed": indexed,
            "total": len(annotated), "returned_count": len(items),
            "next_cursor": next_cursor, "items": items,
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

    def export_research(self, company_ref: str, format: str) -> dict[str, Any]:
        """Download a read-only research artifact in the current mission scope."""
        from .cockpit_research_export import export_download

        ref = _text(company_ref, "company_ref", maximum=512)
        if format not in {"html", "xlsx"}:
            raise CockpitError("请选择 HTML 报告或 Excel 模型")
        with self._core() as core:
            mission = self._mission(core)
            member = next((member for member in mission["universe"]
                           if member["company_ref"] == ref), None)
            if member is None:
                raise CockpitError("公司不在当前研究任务范围内")
        try:
            return export_download(self.config.core_db, ref, format,
                                   mission_ref=mission["mission_ref"],
                                   company_label="_".join(str(member[key])
                                       for key in ("ticker", "name") if member.get(key)))
        except (ValueError, RuntimeError, OSError, sqlite3.Error) as exc:
            raise CockpitError(f"无法导出：{exc}") from exc

    def research_library(self, company_ref: str) -> dict[str, Any]:
        from .cockpit_research_library import research_library

        ref = _text(company_ref, "company_ref", maximum=512)
        with self._core() as core:
            try:
                result = research_library(core, self._mission(core), ref)
            except ValueError as exc:
                raise CockpitError(str(exc)) from exc
        return {"as_of": _iso(self.clock()), **result}

    def company_model(self, company_ref: str) -> dict[str, Any]:
        """One company's forecast model, printed so a person can argue with it.

        The table is P13-M2's own renderer rather than a second layout here:
        it prints every assumption's ``because`` under the assumption and
        every unavailable result's reason where its number would be, and a
        cockpit-local re-rendering would lose exactly those two things.
        """

        ref = _text(company_ref, "company_ref", maximum=512)
        from .company_model_report import render_forecast_model
        from .cockpit_model_display import readiness_labels
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
            # P17b: a version that was refused never reached the table above,
            # so the page would otherwise show the last version that *did*
            # publish with nothing saying a newer one was stopped and why.
            refused = self._invariants(core).get(ref) or {}
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
        try:
            from .research_localization_store import load_ui_texts
            model_text = load_ui_texts(self.config.core_db)
        except (ImportError, OSError, sqlite3.Error, ValueError, TypeError):
            model_text = {}
        policy = _load_json(self.config.core_db.parent / "research-language-policy.json") or {}
        review_required = isinstance(policy, Mapping) and policy.get("required") is True
        def display_model_text(value: str) -> str:
            if value in model_text:
                return model_text[value]
            from .cockpit_model_display import native_chinese_model_text
            native = native_chinese_model_text(value)
            if native is not None:
                return native
            return ("正文正在检查文字表达，完成后会显示。"
                    if review_required else value)
        return {
            "as_of": _iso(self.clock()), "company_ref": ref, "company": label,
            "version": record.get("version"), "version_ref": record.get("id"),
            "created_at": record.get("created_at"),
            "change_reason": record.get("change_reason"),
            "change_reason_label": CHANGE_REASON_LABELS.get(
                record.get("change_reason"), record.get("change_reason")),
            "decision": record.get("decision"),
            "readiness": readiness_labels(record, readiness, display_model_text),
            "note": self._forecast_note(readiness),
            "table": render_forecast_model(
                record, entity_name=label,
                display_text=display_model_text, include_technical=False),
            "technical_table": render_forecast_model(record, entity_name=label),
            "history": history,
            "invariants": refused,
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
            try:
                mission = self._mission(core)
            except CockpitMissionMissing:
                catalog = self._shared_connections() or {"sources": []}
                rows = []
                for item in catalog["sources"]:
                    slug = item["connector_ref"].split(":", 1)[-1]
                    label = {
                        "alphaengine-library": "卖方研报与电话会 · 资料检索",
                        "guidepoint-library": "Guidepoint 专家访谈 · 资料检索",
                        "sec-edgar": "SEC 财报与公告",
                        "host-tool:company-wiki:get_document": "公司知识库 · 文档读取",
                        "host-tool:company-wiki:list_documents": "公司知识库 · 资料目录",
                        "host-tool:sales-notes:get_note": "卖方销售快报 · 内容读取",
                        "host-tool:sales-notes:list_notes": "卖方销售快报 · 快报目录",
                    }.get(slug, SOURCE_SLUG_LABELS.get(slug, SOURCE_LABELS.get(
                        item["connector_ref"], "资料来源配置")))
                    rows.append({"slug": slug, "source_ref": item["connector_ref"],
                                 "label": label,
                                 "content": [SOURCE_OPERATION_LABELS.get(op, "资料读取")
                                             for op in item["allowed_operations"]],
                                 "connector": {"status": "installed", "status_label": "共用连接已登记"},
                                 "mission": {"status": "undeclared", "status_label": "本环境尚未授权"},
                                 "quotas": [], "cadence_label": "待设置研究目标"})
                return {"sources": rows, "routing": {"available": False,
                            "reason": "本环境尚未配置研究调用"},
                        "policy_note": "这里只共用资料来源的连接，资料和研究审批保存在各自的研究环境。"}
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
                "label": SOURCE_SLUG_LABELS.get(
                    entry["slug"], SOURCE_LABELS.get(entry["source_ref"], entry["slug"])),
                # 能取什么
                "content": [CONTENT_KIND_LABELS.get(kind, kind)
                            for kind in entry["content_kinds"]],
                # 层级
                "tier": tier, "tier_label": EVIDENCE_TIER_LABELS.get(tier, tier),
                # 状态
                "status": status,
                "status_label": CONNECTION_STATUS_LABELS.get(status, status),
                "connector": {
                    "installed": bool(entry["in_inventory"]),
                    "status": "installed" if entry["in_inventory"] else "not_installed",
                    "status_label": ("本机连接器目录已登记" if entry["in_inventory"]
                                     else "本机连接器目录未登记"),
                },
                "mission": {
                    "declared": status != "undeclared",
                    "permitted": status in {"connected", "probe_only"},
                    "status": status,
                    "status_label": CONNECTION_STATUS_LABELS.get(status, status),
                },
                "installed": entry["in_inventory"],
                "installed_note": (None if entry["in_inventory"]
                                   else "这条来源还没装进目录"),
                # 配额: the map carries each row as sorted item pairs so the
                # projection can be hashed; a dict is what a page renders.
                "quotas": [
                    {"operation": row.get("operation"),
                     "operation_label": SOURCE_OPERATION_LABELS.get(
                         row.get("operation"), row.get("operation")),
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

    # -- P14-M2: the 「模型」 page ------------------------------------------------

    @staticmethod
    def _model_display_name(profile_id: str,
                            catalogue: Mapping[str, Mapping[str, Any]]) -> str:
        """Use registered metadata for prose while retaining the ID separately."""
        profile = catalogue.get(profile_id)
        if not isinstance(profile, Mapping):
            return "未登记模型"
        model = str(profile.get("model") or "").strip()
        family = str(profile.get("family") or "").strip()
        provider = str(profile.get("provider") or "").strip()
        provider_label = {
            "openai": "OpenAI", "anthropic": "Anthropic", "google": "Google",
            "antigravity": "Antigravity",
            "antigravity-cli-gateway": "Antigravity",
            "deepseek": "DeepSeek", "zai": "智谱",
            "claude-cli-gateway": "Claude 网关", "qwen": "Qwen",
            "xai": "xAI", "openrouter": "OpenRouter",
        }.get(provider, "已登记渠道" if provider else "")
        # Classification metadata belongs in model details, not its name.
        # In particular, unclassified:<provider> is an internal status marker.
        base = model or (family if not family.startswith("unclassified:") else "") or "未命名模型"
        return f"{base} · {provider_label}" if provider_label else base

    @staticmethod
    def _model_family_label(value: Any) -> str:
        family = str(value or "").strip()
        labels = {
            "anthropic-claude-5": "Claude 5 系列",
            "deepseek-v4": "DeepSeek V4 系列",
            "google-gemini-3": "Gemini 3 系列",
            "openai-gpt-5.6": "GPT-5.6 系列",
            "openai-gpt-6": "GPT-6 系列",
            "qwen-3.8": "Qwen 3.8 系列",
            "xai-grok-4": "Grok 4 系列",
            "xai-grok-build": "Grok Build 系列",
            "zhipu-glm-5.3": "GLM-5.3 系列",
        }
        return labels.get(family, "未标明")

    @staticmethod
    def _model_capability_labels(values: Any) -> list[str]:
        labels = {
            "research": "研究分析", "research-hard": "复杂研究分析",
            "verify": "独立核验", "adjudicate": "判断裁决",
            "code": "代码分析", "summarize": "摘要整理",
            "extract": "信息抽取", "format": "格式整理",
            "provider-controlled-verify": "受控核验（可复核他人结论）",
        }
        return [labels.get(str(value), "能力说明待补充")
                for value in (values or [])]

    def _shared_connections(self) -> dict[str, Any] | None:
        manifest = os.environ.get("DALTON_WORKSPACE_MANIFEST")
        if not manifest:
            return None
        from .workspace_creation import connection_catalog_projection
        from .workspace import WorkspaceError
        try:
            return connection_catalog_projection(manifest)
        except (WorkspaceError, OSError, ValueError):
            return None

    def _shared_model_view(self) -> dict[str, Any] | None:
        catalog = self._shared_connections()
        if catalog is None or not catalog.get("available"):
            return None
        return {"models": [{"display_name": self._model_display_name(item["id"], {item["id"]: item}),
                            "provider": item["provider"],
                            "family_label": self._model_family_label(item["family"]),
                            "capability_labels": self._model_capability_labels(item["capabilities"])}
                           for item in catalog["models"]],
                "workspace_authorized": False}

    def trajectory(self, *, limit: int = 120, before: str | None = None) -> dict[str, Any]:
        """A turn-aware event ledger plus per-record detail, newest-first.

        Modelled on the deepseek-harness trajectory view: the steps them-
        selves -- every model call with its purpose, chain position, served
        or skip reason and estimated cost; every work-order transition; every
        budget refusal -- as ledger rows an inspector can open, with a
        cursor so the page can page history backward from the tail.
        """

        from decimal import Decimal
        from .model_selection import PURPOSE_LABELS

        events: list[dict[str, Any]] = []

        def push(at: str, kind: str, status: str, title: str, *,
                 detail: str | None = None, cost_usd: str | None = None,
                 actor: str | None = None) -> None:
            events.append({
                "at": at, "kind": kind, "status": status, "title": title,
                "detail": detail, "cost_usd": cost_usd, "actor": actor,
            })

        router_path = self._model_router_db()
        if router_path and Path(router_path).is_file():
            try:
                router = sqlite3.connect(f"file:{router_path}?mode=ro", uri=True)
                router.row_factory = sqlite3.Row
                router.execute("PRAGMA busy_timeout = 3000")
                # The same "current version of every profile" the models page
                # shows, read from the router we already hold open; without it
                # every call in this ledger reads 未登记模型.
                catalogue: dict[str, dict[str, Any]] = {}
                for row in router.execute(
                    "SELECT p.profile_json FROM model_endpoint_profile_versions p "
                    "WHERE NOT EXISTS (SELECT 1 FROM model_endpoint_profile_versions newer "
                    "WHERE newer.profile_id=p.profile_id AND newer.version>p.version)"
                ).fetchall():
                    profile = json.loads(row[0])
                    if isinstance(profile, Mapping) and profile.get("id"):
                        catalogue[str(profile["id"])] = profile
                params: list[Any] = []
                where = ""
                if before is not None:
                    where = "WHERE created_at < ?"
                    params.append(before)
                rows = router.execute(
                    "SELECT decision_id,purpose,tier,chain_position,profile_id,"
                    "served,skip_reason,created_at FROM model_route_chain_links "
                    + where + " ORDER BY link_sequence DESC LIMIT ?",
                    [*params, limit],
                ).fetchall()
                costs: dict[str, str] = {}
                decisions = router.execute(
                    "SELECT decision_id,decision_json FROM model_route_decisions "
                    "ORDER BY decision_sequence DESC LIMIT 400"
                ).fetchall()
                for row in decisions:
                    record = json.loads(row["decision_json"])
                    selected = record.get("selected_profile_version_ref")
                    if not selected:
                        continue
                    for candidate in record.get("candidate_snapshot") or []:
                        if candidate.get("profile_version_ref") == selected:
                            costs[row["decision_id"]] = str(
                                Decimal(str(candidate.get("estimated_cost_usd") or 0))
                            )
                            break
                for row in rows:
                    purpose = str(row["purpose"] or row["tier"] or "")
                    label = PURPOSE_LABELS.get(purpose, purpose)
                    model = (self._model_display_name(str(row["profile_id"] or ""), catalogue)
                             or str(row["profile_id"] or ""))
                    cost = costs.get(str(row["decision_id"]))
                    if row["served"]:
                        push(str(row["created_at"]), "call", "ok",
                             f"{label} → {model}（第 {row['chain_position']} 顺位）",
                             cost_usd=cost, actor=purpose)
                    else:
                        reason = str(row["skip_reason"] or "skipped")
                        push(str(row["created_at"]), "call", "skip",
                             f"{label}：{model} 未承接",
                             detail=f"原因：{SKIP_REASON_LABELS.get(reason, reason)}",
                             actor=purpose)
                router.close()
            except (sqlite3.Error, OSError, ValueError):
                pass

        try:
            scheduler = sqlite3.connect(
                f"file:{self.config.scheduler_db}?mode=ro", uri=True)
            scheduler.row_factory = sqlite3.Row
            scheduler.execute("PRAGMA busy_timeout = 3000")
            params: list[Any] = []
            where = ""
            if before is not None:
                where = "WHERE created_at < ?"
                params.append(before)
            rows = scheduler.execute(
                "SELECT work_order_id,state,reason,created_at "
                "FROM scheduler_attempt_events " + where
                + " ORDER BY event_seq DESC LIMIT ?",
                [*params, max(20, limit // 3)],
            ).fetchall()
            for row in rows:
                who = str(row["work_order_id"] or "")
                if ":" in who:
                    who = who.split(":", 1)[1]
                    who = who.split("-", 1)[-1] if "-" in who else who
                state = str(row["state"] or "")
                push(str(row["created_at"]), "work",
                     "failed" if state == "failed"
                     else "ok" if state == "succeeded" else "run",
                     f"工单{WORK_STATE_LABELS.get(state, state)}：{who[:44]}",
                     detail=str(row["reason"] or "") or None)
            scheduler.close()
        except (sqlite3.Error, OSError, ValueError):
            pass

        budget_db = Path(self.config.state_dir) / "thesis-impact-budget.sqlite"
        if budget_db.is_file():
            try:
                budget = sqlite3.connect(f"file:{budget_db}?mode=ro", uri=True)
                budget.row_factory = sqlite3.Row
                budget.execute("PRAGMA busy_timeout = 3000")
                day = self.clock().astimezone(timezone.utc).date().isoformat()
                params: list[Any] = [day]
                where = "WHERE day=?"
                if before is not None:
                    where += " AND created_at < ?"
                    params.append(before)
                rows = budget.execute(
                    "SELECT record_json FROM thesis_impact_day_rejections "
                    + where + " ORDER BY rowid DESC LIMIT ?",
                    [*params, 30],
                ).fetchall()
                for row in rows:
                    record = json.loads(row["record_json"])
                    reason = record.get("reason") or "refused"
                    push(str(record.get("created_at") or ""), "budget", "warn",
                         BUDGET_REASON_LABELS.get(reason, reason),
                         detail=(
                             f"预留 ${Decimal(record.get('reserved_micros', 0)) / Decimal(1_000_000):.4f}"
                             f"，已用 ${Decimal(record.get('committed_micros', record.get('spent', 0))) / Decimal(1_000_000):.2f}"
                             f" / 上限 ${Decimal(record.get('cap_micros', 0)) / Decimal(1_000_000):.0f}"))
                budget.close()
            except (sqlite3.Error, OSError, ValueError):
                pass

        events.sort(key=lambda item: item["at"], reverse=True)
        window = events[:limit]
        return {
            "as_of": _iso(self.clock()),
            "events": window,
            "count": len(window),
            "older": window[-1]["at"] if len(window) == limit and len(events) > limit else None,
        }

    def models(self) -> dict[str, Any]:
        """Per calling stage: the tier, the chain it will really use, and a choice.

        Everything on this page is read out of two files this process already
        has permission to read -- the model configuration and, when the Core is
        installed beside a gateway, the OpenClaw configuration -- and out of
        the router database, read-only.  The cockpit holds no write handle: every button
        here goes back out through the writer as the owner's own principal
        (ADR-0006).
        """

        from .model_selection import (
            ModelSelectionError, PURPOSE_LABELS, SELECTION_MODES,
            purpose_policy_bindings,
        )
        from .model_budget_configuration import call_budget_view

        path = self._model_router_db()
        if path is None:
            shared = self._shared_model_view()
            return {"available": False, "shared_catalog": shared,
                    "reason": ("已登记共用模型连接；请先为本环境配置研究目标和调用权限。" if shared else
                               "这台机器上还没有模型路由库，所以没有可选的模型")}
        try:
            bindings = purpose_policy_bindings(
                self.config.state_dir,
                cockpit_model_config_path=self.config.model_config_path,
            )
        except (ModelSelectionError, OSError, ValueError) as exc:
            return {"available": False, "reason": f"模型配置读不出来：{_reason(exc)}"}
        broker = (None if self.config.openclaw_config_path is None
                  else _load_json(self.config.openclaw_config_path))
        broker = broker if isinstance(broker, Mapping) else None
        from .model_fallback_chain import FallbackChainError, routing_overview
        from .model_router import ModelRouter, ModelRouterError
        from .openclaw_model_discovery import discover_models

        try:
            with closing(ModelRouter(path, read_only=True)) as router:
                overview = routing_overview(
                    router, openclaw_config=broker, checked_at=self.clock())
                discovery = (
                    {} if broker is None
                    else discover_models(broker, router=router,
                                         checked_at=self.clock())
                )
                notices = router.fallback_notices()
                catalogue = {
                    profile["id"]: profile for profile in router.latest_profiles()
                }
        except (FallbackChainError, sqlite3.Error, OSError, ValueError) as exc:
            return {"available": False, "reason": f"模型目录读不出来：{_reason(exc)}"}
        purposes = []
        default_rows = {row["purpose"]: row for row in overview["purposes"]}
        bound_views: dict[tuple[str, str], tuple[dict[str, Any], dict[str, Any],
                                                dict[str, dict[str, Any]]]] = {}
        for purpose in sorted(default_rows):
            binding = bindings.get(purpose, {
                "status": "unconfigured", "source": "dynamic launch argument",
                "policy_version_ref": None,
            })
            policy_ref = binding.get("policy_version_ref")
            bound_db = binding.get("model_router_db")
            selected = pinned = bound_catalogue = None
            binding_error = None
            if isinstance(policy_ref, str) and isinstance(bound_db, str):
                key = (str(Path(bound_db).expanduser().resolve()), policy_ref)
                try:
                    if key not in bound_views:
                        with closing(ModelRouter(key[0], read_only=True)) as bound_router:
                            bound_policy = bound_router.get_policy(policy_ref)
                            bound_overview = routing_overview(
                                bound_router, openclaw_config=broker,
                                checked_at=self.clock(), policy_version_ref=policy_ref)
                            bound_catalog = {
                                profile["id"]: profile
                                for profile in bound_router.latest_profiles()
                            }
                        bound_views[key] = (bound_overview, bound_policy, bound_catalog)
                    selected, pinned, bound_catalogue = bound_views[key]
                except (FallbackChainError, ModelRouterError, sqlite3.Error,
                        OSError, ValueError) as exc:
                    binding_error = _reason(exc)
            if binding.get("status") == "deterministic":
                row = {**default_rows[purpose], "mode": "deterministic", "chain": [],
                       "superseded_chain": [], "last_served": None}
            elif selected is None:
                row = {**default_rows[purpose], "mode": "unconfigured", "chain": [],
                       "superseded_chain": [], "last_served": None}
            else:
                row = next(item for item in selected["purposes"]
                           if item["purpose"] == purpose)
                override = (pinned.get("purpose_overrides") or {}).get(purpose)
                tiers = (pinned.get("fallback_chains") or {}).get("tiers")
                allowed = (pinned.get("filters") or {}).get("allowed_profile_ids") or []
                # Legacy policies are direct pins: their consumer calls the
                # router without a tier chain.  Showing the global tier here
                # would advertise models this policy will actually reject.
                if not isinstance(override, Mapping) and not tiers and allowed:
                    row = {**row, "mode": ("pinned" if len(allowed) == 1
                                            else "candidate_set"),
                           "superseded_chain": None, "chain": [{
                               "position": position, "profile_id": profile_id,
                               "registered": profile_id in bound_catalogue,
                               "status": (bound_catalogue.get(profile_id) or {}).get("status", "live"),
                               "family": (bound_catalogue.get(profile_id) or {}).get("family"),
                               "unpriced": bool((bound_catalogue.get(profile_id) or {}).get("unpriced")),
                           } for position, profile_id in enumerate(allowed, 1)]}
            chain = [{
                "position": link["position"], "model": link["profile_id"],
                "display_name": self._model_display_name(
                    link["profile_id"], (bound_catalogue
                                         if bound_catalogue is not None else catalogue)),
                "family": link["family"], "unpriced": link["unpriced"],
                "retired": link["status"] == "retired",
                "note": (
                    "这台机器上没有这个模型的档案" if not link["registered"]
                    else "已退役：网关不再提供" if link["status"] == "retired"
                    else "未定价：只能当最后的回退" if link["unpriced"]
                    else None
                ),
            } for link in row["chain"]]
            served = row["last_served"]
            purposes.append({
                "purpose": row["purpose"],
                "label": PURPOSE_LABELS.get(row["purpose"], row["purpose"]),
                "tier": row["tier"],
                "tier_label": MODEL_TIER_LABELS.get(row["tier"], row["tier"]),
                "mode": row["mode"],
                "mode_label": MODEL_SELECTION_MODE_LABELS.get(
                    row["mode"], ({"unconfigured": "未配置", "deterministic": "确定性处理",
                                   "pinned": "固定模型",
                                   "candidate_set": "策略候选集合"}
                                  .get(row["mode"], row["mode"]))
                ),
                "configuration_status": ("error" if binding_error else binding["status"]),
                "configuration_error": binding_error,
                "configuration_source": binding["source"],
                "policy_version_ref": policy_ref,
                "requires_restart": bool(binding.get("requires_restart")),
                "editable": bool(binding.get("editable")),
                "call_budget": call_budget_view(self.config.state_dir, purpose, binding=binding),
                "run_budget": call_budget_view(self.config.state_dir, purpose, binding=binding, kind="run"),
                "chain": chain,
                "superseded_chain": row["superseded_chain"],
                "superseded_display_names": [
                    self._model_display_name(
                        profile_id, (bound_catalogue
                                     if bound_catalogue is not None else catalogue))
                    for profile_id in (row["superseded_chain"] or [])
                ],
                "superseded_note": (
                    None if not row["superseded_chain"] else
                    "指定模型均已退役，当前暂用该环节的默认模型顺序："
                    + "、".join(
                        self._model_display_name(
                            profile_id, (bound_catalogue
                                         if bound_catalogue is not None else catalogue))
                        for profile_id in row["superseded_chain"])
                ),
                "last_served": None if served is None else {
                    "model": served["profile_id"],
                    "display_name": self._model_display_name(
                        served["profile_id"], (bound_catalogue
                                               if bound_catalogue is not None else catalogue)),
                    "position": served["chain_position"],
                    "at": served["created_at"],
                    "cost_usd": served["estimated_cost_usd"],
                },
                "last_served_note": (
                    "该环节尚无调用记录" if served is None else
                    (f"上一次是链上第 {served['chain_position']} 个模型服务的"
                     + (f"，估算 {served['estimated_cost_usd']} 美元"
                        if served["estimated_cost_usd"] else ""))
                ),
            })
        # Everything live, in one list, because a selector that only offered
        # what is already in some chain could never be used to choose a model
        # the catalog lane has just registered -- which is the whole point.
        choices = sorted(
            (
                {
                    "model": profile_id,
                    "display_name": self._model_display_name(profile_id, catalogue),
                    "provider": profile.get("provider"),
                    "model_ref": profile.get("model"),
                    "profile_version_ref": profile.get("profile_version_ref"),
                    "profile_hash": profile.get("content_hash"),
                    "family": profile.get("family"),
                    "family_label": self._model_family_label(profile.get("family")),
                    "capabilities": list(profile.get("capabilities") or []),
                    "capability_labels": self._model_capability_labels(
                        profile.get("capabilities")),
                    "unpriced": bool(profile.get("unpriced")),
                    "verifier_eligible": (
                        "provider-controlled-verify" in (profile.get("capabilities") or [])
                        and not str(profile.get("family") or "").startswith("unclassified:")
                    ),
                    "note": (
                        "未声明可核验的模型家族：不能承担独立核验"
                        if str(profile.get("family") or "").startswith("unclassified:")
                        else "缺少受控计数：不能放在独立复核链里"
                        if "provider-controlled-verify" not in (profile.get("capabilities") or [])
                        and "verify" in (profile.get("capabilities") or [])
                        else "未定价：只能放在链的最后一位"
                        if profile.get("unpriced") else None
                    ),
                }
                for profile_id, profile in catalogue.items()
                if profile.get("status") != "retired"
            ),
            key=lambda item: item["model"],
        )
        # 2026-09-15: the owner edits one chain per tier. The card's chain is
        # the tier's own chain as the pinned policies declare it -- read from a
        # purpose that follows the tier, so a per-purpose override never
        # masquerade as the tier's choice.
        tier_cards: dict[str, dict[str, Any]] = {}
        for row in purposes:
            tier = row["tier"]
            card = tier_cards.setdefault(tier, {
                "tier": tier,
                "label": row["tier_label"],
                "chain": [],
                "requires_restart": False,
                "purposes": [],
            })
            card["purposes"].append({
                "purpose": row["purpose"], "label": row["label"],
                "mode": row["mode"], "mode_label": row["mode_label"],
                "requires_restart": row["requires_restart"],
                "editable": row["editable"],
            })
            card["requires_restart"] = card["requires_restart"] or row["requires_restart"]
            if not card["chain"] and row["mode"] == "tier":
                card["chain"] = list(row["chain"])
        from .model_fallback_chain import CHAIN_ELIGIBILITY_ENFORCED
        if not CHAIN_ELIGIBILITY_ENFORCED:
            # The owner's 2026-09-15 direction: every model may join every
            # tier's chain.  The capability facts stay on each choice so the
            # picker can still say what a model declares.
            for choice in choices:
                choice["verifier_eligible"] = True
                if choice["note"] in (
                    "未声明可核验的模型家族：不能承担独立核验",
                    "缺少受控计数：不能放在独立复核链里",
                ):
                    choice["note"] = None
        return {
            "available": True,
            "as_of": _iso(self.clock()),
            "schema_version": SCHEMA_VERSION,
            "eligibility_enforced": CHAIN_ELIGIBILITY_ENFORCED,
            "policy_version_ref": None,
            "purposes": purposes,
            "tier_cards": sorted(tier_cards.values(), key=lambda card: card["tier"]),
            "modes": [{"value": mode,
                       "label": MODEL_SELECTION_MODE_LABELS.get(mode, mode)}
                      for mode in SELECTION_MODES],
            "choices": choices,
            "catalog": self._model_catalog(discovery, broker is not None),
            "notices": [{
                "ref": notice["id"], "at": notice["created_at"],
                "message": notice["message"],
                "purpose": notice["purpose"],
                "acknowledged": notice["acknowledged_by"] is not None,
            } for notice in notices],
            "tiers": overview["tiers"],
        }

    @staticmethod
    def _model_catalog(discovery: Mapping[str, Any], configured: bool) -> dict[str, Any]:
        """The three diff sets, named in the owner's words."""

        if not configured or not discovery:
            return {"available": False,
                    "reason": "尚未配置模型网关文件位置，暂时无法读取模型目录"}
        return {
            "available": True,
            "in_sync": bool(discovery.get("in_sync")),
            "in_openclaw_not_allowed": list(discovery["in_openclaw_not_allowed"]),
            "in_openclaw_not_allowed_note":
                "模型网关已提供，但 Dalton 尚未获准使用；可通过「允许使用」提交授权操作",
            "allowed_not_in_dalton": list(discovery["allowed_not_in_dalton"]),
            "allowed_not_in_dalton_note":
                "Dalton 已获授权，但本机尚未登记模型档案；同步任务会按计划自动登记",
            "dalton_not_in_openclaw": list(discovery["dalton_not_in_openclaw"]),
            "dalton_not_in_openclaw_note":
                "本机仍有模型档案，但模型网关已不再提供；同步任务会将档案标记为退役并保留记录",
            "allowed_without_broker_profile":
                list(discovery.get("allowed_without_broker_profile") or []),
        }

    def _routing(self) -> dict[str, Any]:
        """P14-M: which model serves each purpose, and what it falls back to."""

        config = (_load_json(self.config.model_config_path)
                  if self.config.model_config_path is not None else None)
        path = (config or {}).get("model_router_db") if isinstance(config, dict) else None
        if not path or not Path(str(path)).is_file():
            return {"available": False,
                    "reason": "本机尚未配置模型路由库，暂时无法读取路由"}
        from .model_fallback_chain import FallbackChainError, routing_overview
        from .model_selection import PURPOSE_LABELS
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
                catalogue = {
                    profile["id"]: profile for profile in router.latest_profiles()
                }
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
                    "display_name": self._model_display_name(
                        link["profile_id"], catalogue),
                    "registered": link["registered"], "status": link["status"],
                    "family": link["family"],
                    "note": (None if link["registered"]
                             else "这台机器上没有这个模型的档案"),
                } for link in entry["chain"]],
                "last_served": None if served is None else {
                    "model": served["profile_id"],
                    "display_name": self._model_display_name(
                        served["profile_id"], catalogue),
                    "position": served["chain_position"],
                    "purpose": served["purpose"],
                    "purpose_label": PURPOSE_LABELS.get(
                        served["purpose"], "未登记用途"),
                    "at": served["created_at"],
                },
                "last_served_note": (
                    "还没有用过这一层" if served is None else
                    (f"上一次是链上第 {served['chain_position']} 个模型服务的"
                     + ("（也就是第一选择）" if served["chain_position"] == 1
                        else "——第一选择当时没答上"))),
                "skipped_since_last_served": [
                    {"model": link["profile_id"],
                     "display_name": self._model_display_name(
                         link["profile_id"], catalogue),
                     "reason": link["skip_reason"],
                     "reason_label": {
                         "transport_failure": "连接未成功",
                         "content_refusal": "模型未返回可用内容",
                         "budget_refused": "本次请求超出适用额度",
                         "verifier_not_independent": "不满足独立复核要求",
                         "unclassified": "未能确认跳过原因",
                     }.get(link["skip_reason"], "未能确认跳过原因")}
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
            try:
                mission = self._mission(core)
            except CockpitMissionMissing:
                return {"as_of": _iso(self.clock()), "available": False,
                        "reason": "本环境尚未开始研究，暂无每周复盘"}
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
            "historical": True,
            "content_as_of": rows[0]["created_at"],
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
            # W4 / Chem §3.3: 不动也要能被评价. Counts only -- the rows behind
            # them are derived by a frozen formula with no model in it, so the
            # page can show them without the "a model said this" caveat every
            # other judgement on this page carries.
            "judgement_outcomes": _judgement_outcome_panel(
                (record.get("metrics") or {}).get("judgement_outcomes")
            ),
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

    # -- P14-M2: the three model decisions, all through the writer ---------------

    def _governance(self, login: str, operation: str, params: dict[str, Any],
                    *, failure: str) -> dict[str, Any]:
        """One writer call as the owner's own principal (ADR-0006).

        Same shape as the feedback button's: a refusal by contract reads as 400
        so the page can say what to change, and a conflict reads as 409 so it
        can offer a retry that might work.
        """

        actor = _subject_for_login(login)
        try:
            result = self.governance_call(
                self.token_config, self.writer_socket, actor_ref=actor,
                operation=operation, params=params)
        except RemoteError as exc:
            if getattr(exc, "code", None) in {"rejected", "protocol_error", "forbidden"}:
                raise CockpitError(f"{failure}：{_reason(exc)}") from exc
            raise CockpitConflict(f"{failure}：{_reason(exc)}") from exc
        except GovernanceCliError as exc:
            raise CockpitConflict(f"{failure}：{_reason(exc)}") from exc
        return result if isinstance(result, dict) else {"status": "done"}

    def set_call_budget(self, login: str, value: Mapping[str, Any]) -> dict[str, Any]:
        from .call_budget import validate_budget_overrides, validate_run_budget_overrides
        if not isinstance(value, Mapping):
            raise CockpitError("预算配置必须是一个对象")
        purpose = _text(value.get("purpose"), "purpose", maximum=64)
        kind = value.get("kind", "call")
        if kind not in {"call", "run"}:
            raise CockpitError("预算类型必须是单次调用或每轮任务")
        try:
            budget = (validate_budget_overrides if kind == "call" else validate_run_budget_overrides)(value.get("budget"))
        except ValueError as exc:
            raise CockpitError(str(exc)) from exc
        if (kind == "call" and "max_cost_usd" in budget
                and value.get("expected_shared_policy_hash") is not None):
            if set(budget) != {"max_cost_usd"}:
                raise CockpitError("共享费用上限与本环境 token/超时预算请分开保存")
            manager = self.config.workspace_manager_config_path
            if manager is None:
                raise CockpitError("共享模型费用管理尚未配置")
            from .workspace_manager import request_set_shared_call_budget
            result = request_set_shared_call_budget(
                manager, login, purpose, budget["max_cost_usd"],
                _text(value.get("expected_shared_policy_hash"), "共享策略版本", maximum=64))
            self.journal.record_event(kind="model_budget", title=f"已调整所有环境「{purpose}」的单次费用上限",
                detail=json.dumps(budget, ensure_ascii=False), login=login,
                refs={"purpose": purpose, "revision": result["policy"]["revision"]})
            return result
        result = self._governance(login, "set_model_call_budget", {
            "purpose": purpose, "budget": budget, "kind": kind,
            "expected_config_hash": _text(value.get("expected_config_hash"), "配置版本", maximum=64),
        }, failure="预算没有保存")
        self.journal.record_event(kind="model_budget", title=f"已调整「{purpose}」的调用预算",
                                  detail=json.dumps(budget, ensure_ascii=False), login=login,
                                  refs={"purpose": purpose, "revision": result.get("revision")})
        return result

    def set_research_budget(self, login: str, value: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise CockpitError("研究预算必须是一个对象")
        budget = value.get("budget")
        required = {"max_daily_paid_calls", "max_daily_cost_usd", "max_alphaengine_calls_24h"}
        optional = {"pools_enforcement", "max_daily_document_reads"}
        if not isinstance(budget, Mapping) or not required <= set(budget) or not set(budget) <= required | optional:
            raise CockpitError("研究预算字段不完整")
        if "pools_enforcement" in budget and budget["pools_enforcement"] not in ("on", "off"):
            raise CockpitError("分项预算池的执行开关只接受 on 或 off")
        result = self._governance(login, "set_research_budget_authority_chain", {
            "mission_ref": _text(value.get("mission_ref"), "mission_ref", maximum=160),
            "budget": dict(budget),
            "expected_mission_hash": _sha(value.get("expected_mission_hash"), "mission hash"),
        }, failure="研究预算没有保存")
        self.journal.record_event(kind="model_budget", title="已同步更新完整研究预算授权链",
            detail=json.dumps(budget, ensure_ascii=False), login=login,
            refs={"mission_version_ref": result.get("mission")})
        return result

    def select_model(self, login: str, value: Mapping[str, Any]) -> dict[str, Any]:
        """P14-M2: the owner points one calling stage at a model, or at its tier.

        2026-09-15: a ``tier`` key edits the whole tier (高阶推理 / 批量阅读 /
        独立复核) in one save instead of one stage at a time.
        """

        from .model_selection import PURPOSE_LABELS, SELECTION_MODES

        if not isinstance(value, Mapping):
            raise CockpitError("选择必须是一个对象")
        tier = value.get("tier")
        mode = _text(value.get("mode"), "mode", maximum=32)
        if mode not in SELECTION_MODES:
            raise CockpitError("请选择“使用系统推荐配置”或“手动指定模型”")
        chain = value.get("chain") or []
        if not isinstance(chain, list) or any(
            not isinstance(item, str) for item in chain
        ):
            raise CockpitError("手动指定的模型必须是模型标识列表")
        if mode == "explicit" and not chain:
            raise CockpitError("手动指定模型时至少需要一个模型，并按调用顺序排列")
        if tier is not None:
            from .model_fallback_chain import purpose_tiers
            tiers = {held for held in purpose_tiers().values()}
            tier = _text(tier, "tier", maximum=32)
            if tier not in tiers:
                raise CockpitError("模型类别必须是高阶推理、批量阅读或独立复核")
            params: dict[str, Any] = {"tier": tier, "mode": mode}
            if mode == "explicit":
                params["chain"] = [_text(item, "chain[]", maximum=256) for item in chain]
            result = self._governance(
                login, "set_model_selection", params, failure="这个选择没有生效")
            self.journal.record_event(
                kind="model_selection", title=f"你给「{MODEL_TIER_LABELS[tier]}」整类选了模型",
                detail=("使用系统推荐配置" if mode == "tier"
                        else " → ".join(chain)),
                login=login, refs={"tier": tier, "mode": mode,
                                   "profile_ids": ",".join(chain)})
            return {**result, "tier": tier, "label": MODEL_TIER_LABELS[tier]}
        purpose = _text(value.get("purpose"), "purpose", maximum=64)
        params = {"purpose": purpose, "mode": mode}
        if mode == "explicit":
            params["chain"] = [_text(item, "chain[]", maximum=256) for item in chain]
        result = self._governance(
            login, "set_model_selection", params, failure="这个选择没有生效")
        label = PURPOSE_LABELS.get(purpose, purpose)
        display_chain: list[str] = []
        if mode == "explicit":
            path = self._model_router_db()
            catalogue: dict[str, dict[str, Any]] = {}
            if path is not None:
                from .model_router import ModelRouter
                try:
                    with closing(ModelRouter(path, read_only=True)) as router:
                        catalogue = {
                            profile["id"]: profile for profile in router.latest_profiles()
                        }
                except (sqlite3.Error, OSError, ValueError):
                    catalogue = {}
            display_chain = [self._model_display_name(profile_id, catalogue)
                             for profile_id in chain]
        self.journal.record_event(
            kind="model_selection", title=f"你给「{label}」选了模型",
            detail=("使用系统推荐配置" if mode == "tier"
                    else " → ".join(display_chain)),
            login=login, refs={"purpose": purpose, "mode": mode,
                               "profile_ids": ",".join(chain)})
        return {**result, "purpose": purpose, "label": label}

    def allow_model(self, login: str, value: Mapping[str, Any]) -> dict[str, Any]:
        """P14-M2: 「允许使用」 -- let one of the gateway's models through to Dalton."""

        if not isinstance(value, Mapping):
            raise CockpitError("模型授权请求必须是一个对象")
        model_ref = _text(value.get("model_ref"), "model_ref", maximum=256)
        result = self._governance(
            login, "allow_openclaw_model", {"model_ref": model_ref},
            failure="这个模型尚未获准使用")
        self.journal.record_event(
            kind="model_allow", title="你已允许使用一个模型",
            detail=result.get("reload_instruction"), login=login,
            refs={"model_ref": model_ref,
                  "backup_path": result.get("backup_path") or ""})
        return result

    def declare_model_metadata(
        self, login: str, value: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Record owner-declared family/capabilities inside Dalton."""

        if not isinstance(value, Mapping):
            raise CockpitError("模型信息必须是一个对象")
        profile_id = _text(value.get("profile_id"), "profile_id", maximum=256)
        profile_version_ref = _text(
            value.get("profile_version_ref"), "profile_version_ref", maximum=256)
        profile_hash = _text(value.get("profile_hash"), "profile_hash", maximum=128)
        family = _text(value.get("family"), "family", maximum=128)
        capabilities = value.get("capabilities")
        if not isinstance(capabilities, list) or not capabilities or any(
            not isinstance(item, str) or not item.strip() for item in capabilities
        ):
            raise CockpitError("能力必须是至少一个名称")
        capabilities = [
            _text(item.strip(), "capabilities[]", maximum=64)
            for item in capabilities
        ]
        result = self._governance(
            login, "declare_model_profile_metadata",
            {"profile_id": profile_id, "family": family,
             "capabilities": capabilities,
             "profile_version_ref": profile_version_ref,
             "profile_hash": profile_hash},
            failure="模型信息没有保存",
        )
        declaration = result.get("declaration") or {}
        self.journal.record_event(
            kind="model_metadata", title="你补充了模型信息",
            detail=(f"模型家族：{self._model_family_label(family)}；能力："
                    f"{'、'.join(self._model_capability_labels(capabilities))}"),
            login=login,
            refs={"profile_id": profile_id,
                  "declaration_ref": declaration.get("declaration_ref") or ""},
        )
        return result

    def acknowledge_model_notice(
        self, login: str, value: Mapping[str, Any]
    ) -> dict[str, Any]:
        """P14-M2: 「知道了」 on one "the model you were using has gone"."""

        if not isinstance(value, Mapping):
            raise CockpitError("请求必须是一个对象")
        notice_ref = _text(value.get("ref"), "ref", maximum=256)
        result = self._governance(
            login, "acknowledge_model_fallback_notice", {"notice_id": notice_ref},
            failure="这条提示没有被记下")
        return {**result, "ref": notice_ref}

    # -- jobs (model work off the request thread) -----------------------------------------

    def _model_instance(self) -> CockpitModel:
        if self._model is not None:
            return self._model
        if self.config.model_config_path is None:
            raise CockpitError("问答与目标拆解所需的模型尚未接入")
        config = _load_json(self.config.model_config_path)
        if not isinstance(config, dict):
            raise CockpitError("模型配置无法读取")
        # Interactive brain-tier work can carry the full governed research
        # context, so the plane owns a larger fallback than CockpitModel's
        # general-purpose default. Explicit config budgets still take priority.
        factory = self._model_factory or (
            lambda c: CockpitModel(
                c,
                scheduler_db=self.config.scheduler_db,
                max_cost_usd=COCKPIT_DEFAULT_MAX_COST_USD,
            )
        )
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
        public = {k: v for k, v in job.items() if not k.startswith("_")}
        if public.get("kind") == "ask":
            public["result"] = _answer_citation_period_labels(public.get("result"))
        return public

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
        result = None if row["result_json"] is None else json.loads(row["result_json"])
        if row["kind"] == "ask":
            result = _answer_citation_period_labels(result)
        return {"job_id": row["job_id"], "kind": row["kind"], "login": row["login"], "status": row["status"],
                "request": json.loads(row["request_json"]), "result": result,
                "error": row["error"], "created_at": row["created_at"], "updated_at": row["updated_at"]}

    def history(self, login: str, kind: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.journal.rows("SELECT * FROM cockpit_jobs WHERE kind=? AND login=? ORDER BY created_at DESC LIMIT ?",
                                 (kind, login, max(1, min(int(limit), 100))))
        return [{"job_id": r["job_id"], "status": r["status"], "request": json.loads(r["request_json"]),
                 "result": _answer_citation_period_labels(None if r["result_json"] is None else json.loads(r["result_json"])) if kind == "ask" else (None if r["result_json"] is None else json.loads(r["result_json"])), "error": r["error"],
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
        answer_route_decision_ref = call["route_decision_ref"]

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
                answer_route_decision_ref = second["route_decision_ref"]

        language_review = {"status": "not_configured"}
        reviewed_display_answer = None
        reviewed_display_gaps = None
        reviewed_gap_details = []
        language_root = self.config.core_db.parent
        language_policy = language_root / "research-language-policy.json"
        checker_config = language_root / "research-language-check-model-config.json"
        brain_config = language_root / "research-language-revision-model-config.json"
        verifier_config = language_root / "research-localization-verifier-model-config.json"
        required = False
        if language_policy.exists():
            try:
                policy = json.loads(language_policy.read_text(encoding="utf-8"))
                required = isinstance(policy, Mapping) and policy.get("required") is True
            except (OSError, json.JSONDecodeError):
                raise CockpitError("发布前语言审查配置无法读取，请修复配置后重试")
        configured = checker_config.exists() and brain_config.exists() and verifier_config.exists()
        if configured:
            from .research_language_runtime import run as run_language_review
            product = {"kind": "ask_answer", "version_ref": f"cockpit-ask:{request_id}",
                       "sections": [{"title": "回答", "body": answer["answer"],
                                     "gaps": list(answer["gaps"])}]}
            try:
                language_review = run_language_review(
                    product, mission=mission, request_id=request_id,
                    checker_config=checker_config, brain_config=brain_config, verifier_config=verifier_config,
                    scheduler_db=self.config.scheduler_db, producer_route_decision_ref=answer_route_decision_ref,
                    artifact_dir=language_root / "research-language-reviews" / "ask")
            except Exception as exc:
                raise CockpitError("回答已生成，但发布前语言审查未完成，请稍后重试") from exc
            if language_review.get("status") != "ready_for_publication":
                raise CockpitError("回答已生成，但仍在等待语言审查，尚未发布")
            section = language_review["brain_revision"]["sections"][0]
            from .research_gap_display import ask_gap_display_fields, display_metadata_text
            reviewed_display_answer = display_metadata_text(section["body"])
            gap_fields = ask_gap_display_fields(section["gaps"])
            reviewed_display_gaps = gap_fields["display_gaps"]
            reviewed_gap_details = gap_fields["display_gap_details"]
            cost_micros += int(language_review.get("review_cost_micros") or 0)
            replayed = replayed and (language_review.get("artifact_replayed") is True
                                     or language_review.get("replayed") is True)
        elif required:
            raise CockpitError("回答已生成，但发布前语言审查尚未配置，尚未发布")

        shown = [dict(row) for row in context["shown"]]
        result = {
            "question": question,
            "answer": answer["answer"],
            "display_answer": reviewed_display_answer,
            "display_gaps": reviewed_display_gaps,
            "display_gap_details": reviewed_gap_details,
            "sentences": answer["sentences"],
            # The page's existing citation card reads ``statement``,
            # ``company``, ``period`` and ``at``; those four keep their names
            # so that an answer citing a valuation row renders in the card
            # that already exists. ``block`` and ``block_label`` are what let
            # it say which kind of thing was cited.
            "citations": [{
                "tag": row["tag"], "statement": row["statement"], "ref": row["ref"],
                "period": row["period"], "period_label": _claim_period_label(row["period"]),
                "company": row.get("company") or "",
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
            "language_review": {key: language_review.get(key) for key in
                                ("status", "source_hash", "revision_hash",
                                 "content_hash", "artifact_ref", "artifact_sha256")},
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
        if kind == "goal" and self.workspace_context.get("mode") == "isolated":
            with self._core() as core:
                try:
                    self._mission(core)
                except CockpitMissionMissing:
                    return self._start_job(kind, login, {"text": text, "request_id": request_id},
                        lambda: self._initial_goal_draft(login, text, request_id))
        model = self._model_instance()
        return self._start_job(kind, login, {"text": text, "request_id": request_id},
                               lambda: self._draft(model, login, kind, text, request_id))

    def _initial_goal_draft(self, login: str, text: str, request_id: str) -> dict[str, Any]:
        from .workspace import load_workspace_manifest
        from .workspace_onboarding import initial_goal_draft
        workspace = load_workspace_manifest(os.environ["DALTON_WORKSPACE_MANIFEST"])
        with self._core() as core:
            try:
                self._mission(core)
            except CockpitMissionMissing:
                pass
            else:
                raise CockpitConflict("研究目标已建立，请刷新后再编辑")
        draft_id = "cockpit-draft:initial-goal:" + hashlib.sha256(
            (workspace.workspace_id + "\0" + login + "\0" + request_id).encode()).hexdigest()[:32]
        existing = self.journal.rows("SELECT * FROM cockpit_drafts WHERE draft_id=?", (draft_id,))
        if existing:
            row = existing[0]
            if row["input_text"] != text:
                raise CockpitConflict("这次保存请求已用于另一份目标草稿")
            return {"status": row["status"], "draft_id": draft_id,
                    "draft_hash": row["content_hash"], "kind": "goal",
                    "draft": json.loads(row["draft_json"]), "input_text": text,
                    "cost_usd": 0, "replayed": True, "created_at": row["created_at"]}
        foundation_path = workspace.state_dir / "research-foundation.json"
        call_result: dict[str, Any] = {}
        status = "saved"
        if foundation_path.is_file():
            from .workspace_mission_setup import plan_first_mission_goal
            model = self._model_instance()
            class SetupModel:
                def call_setup(self, **kwargs: Any) -> dict[str, Any]:
                    result = model.call_setup(**kwargs)
                    call_result.update(result)
                    return result
            draft = plan_first_mission_goal(
                SetupModel(), workspace, goal=text,
                method_foundation=json.loads(foundation_path.read_text()),
                request_id=request_id,
                created_at=datetime.fromtimestamp(foundation_path.stat().st_mtime, timezone.utc).isoformat())
            status = "open"
        else:
            draft = initial_goal_draft(workspace, title=text.splitlines()[0][:200], objective=text)
        digest = content_hash(draft)
        now = _iso(self.clock())
        self.journal.write(
            "INSERT OR IGNORE INTO cockpit_drafts(draft_id,kind,login,input_text,draft_json,content_hash,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (draft_id, "goal", login, text, json.dumps(draft, ensure_ascii=False, sort_keys=True),
             digest, status, now, now))
        rows = self.journal.rows("SELECT content_hash,created_at FROM cockpit_drafts WHERE draft_id=?", (draft_id,))
        if rows[0]["content_hash"] != digest:
            raise CockpitConflict("这次保存请求已用于另一份目标草稿")
        return {"status": status, "draft_id": draft_id, "draft_hash": digest, "kind": "goal",
                "draft": draft, "input_text": text,
                "cost_usd": round(call_result.get("cost_micros", 0) / 1_000_000, 4),
                "replayed": call_result.get("replayed", False),
                "created_at": rows[0]["created_at"]}

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
            *final_text_instructions(),
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
            *final_text_instructions(),
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
        if draft.get("schema_version") == "workspace-first-mission-draft-0.1":
            return self._publish_first_mission(login, draft_id, draft)
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

    def _publish_first_mission(self, login: str, draft_id: str, draft: Mapping[str, Any]) -> dict[str, Any]:
        from .workspace import load_workspace_manifest
        workspace = load_workspace_manifest(os.environ["DALTON_WORKSPACE_MANIFEST"])
        foundation = json.loads((workspace.state_dir / "research-foundation.json").read_text())
        if draft.get("setup_state") != "ready_for_confirmation":
            raise CockpitConflict("请补充研究范围后重新整理目标")
        expected = f"coverage-mission-version:{workspace.slug}:{draft['content_hash'][:24]}"
        result = None
        with self._core() as core:
            try:
                committed = self._mission(core)
            except CockpitMissionMissing:
                committed = None
        if isinstance(committed, Mapping) and committed.get("id") == expected:
            result = committed
        elif committed is not None:
            raise CockpitConflict("这个研究环境已经发布了另一份研究目标")
        else:
            try:
                result = self.governance_call(
                    self.token_config, self.writer_socket, actor_ref=_subject_for_login(login),
                    operation="publish_first_workspace_mission", params={
                        "workspace_manifest": json.loads(workspace.manifest_path.read_text()),
                        "method_foundation": foundation, "proposal": dict(draft),
                        "proposal_hash": draft["content_hash"], "actor_ref": _subject_for_login(login),
                    })
            except RemoteError as exc:
                # Publication commits immutable authorities before it writes
                # local source plans. Recover only this draft's deterministic
                # mission version if that bounded follow-up outlives the RPC.
                with self._core() as core:
                    try:
                        committed = self._mission(core)
                    except CockpitMissionMissing:
                        committed = None
                if not isinstance(committed, Mapping) or committed.get("id") != expected:
                    raise CockpitConflict(f"发布没有被接受：{_reason(exc)}") from exc
                result = committed
            except GovernanceCliError as exc:
                raise CockpitConflict(f"发布没有被接受：{_reason(exc)}") from exc
        published = result["id"]
        self.journal.write("UPDATE cockpit_drafts SET status='published', published_ref=?, updated_at=? WHERE draft_id=?",
                           (published, _iso(self.clock()), draft_id))
        self.journal.record_event(
            kind="goal", title="开始研究：" + draft["mission_body"]["title"],
            detail="已确认研究范围、资料来源和预算，后台将开始执行。", login=login,
            refs={"draft_id": draft_id, "mission_version_ref": published})
        return {"status": "published", "draft_id": draft_id,
                "mission_version_ref": published, "version": result.get("version", 1)}


def _reason(exc: BaseException) -> str:
    text = str(exc) or type(exc).__name__
    return text[:300]


def _subject_for_login(login: str) -> str:
    import hashlib
    return "human:tailscale-" + hashlib.sha256(login.encode("utf-8")).hexdigest()[:32]


__all__ = ["CockpitConfig", "CockpitConflict", "CockpitError", "CockpitJournal", "CockpitPlane", "LANE_LABELS"]
