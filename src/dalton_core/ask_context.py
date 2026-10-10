"""Batch G: what an analyst would have open before answering one question.

The 2026-10-09 diagnosis found the question panel reading the same thing for
every question: the newest ~270 number-free Claims, twelve events rendered as
``price_move：price_move`` (no move, no title), and our own NO_CHANGE
judgements, until a 90K-character budget was full.  There was no retrieval by
topic, no date window, and nothing to tell "yesterday" from "last year".  The
owner's ask: 「看怎么才能像个真正的分析师那样，在读了这么多报告之后形成有价值的回答」.

An analyst asked 「ACN 昨天为什么大涨」 does four things, and this module is
those four things, deterministically (no model, no network, read-only):

1. **Pin the date.**  「昨天」 is the previous *trading* day, read off the
   company's own price series (:func:`resolve_dates`); 「上周」, 「上个月」,
   「最近」, 「近 N 天」 and explicit dates resolve to windows the same way.
2. **Read the tape.**  The move itself, computed from the stored series: the
   close, the previous close, the change, the equal-weight move of the covered
   peers the same day and the excess over them, and a broad benchmark when the
   price authority holds one (the ``move`` block, ``M`` tags).  The biggest
   moves of the lookback window travel with it, so an earnings day a week
   earlier is on the page.
3. **Pull the paper.**  Company + question terms + the question type's own
   vocabulary, across every document kind, inside the window
   (:func:`retrieve`, over :func:`evidence_index.evidence_search`), reranked:
   recency measured from the window for move / what-happened questions, kind
   weights per question type (first-hand filings and calls, then the brokers
   and the sales desk, then the web), passages inside the asked-about window
   ahead of the rest.  The passages, the numbers pack and (only when no
   passage exists) the legacy Claims come through
   ``material_pack.build(state_dir, company, "ask", topics=[...])``.
4. **Keep our own view apart.**  Our theses, dossier variant view and event
   judgements are shown in one block, labelled 「我们的看法」, after the
   evidence and capped -- context for the 「我们的看法」 part of the answer and
   never a source for what happened.

Events are rendered with their content (:func:`event_row`): a price move with
its change, the basket and the excess; a document with its kind, house and
title read from the evidence index.

Every row carries a tag (``M1``, ``E2``, ``P7``, ``N3``, ``O1``, ``C4``) and
the ref behind it; ``shown`` is the list the answer's citations are checked
against.  ``sufficiency`` says whether a first-hand passage exists inside the
window and, when not, the MaterialRequest the cockpit should open
(``sufficiency.request``).
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

from .store import content_hash

SCHEMA_VERSION = "0.2"

# The prompt's whole material budget.  Passages are what is trimmed first when
# it is tight (from the bottom of the ranking); the move, the events and our
# own view are small and fixed.
DEFAULT_BUDGET_CHARS = 60_000
#: Kept for the cockpit's own import (``MAX_CLAIMS_IN_PROMPT``): the fallback
#: Claims are capped, never the bulk of a prompt any more.
MAX_CLAIM_ROWS = 60

# ---------------------------------------------------------------------------
# question kinds
# ---------------------------------------------------------------------------

QUESTION_KINDS: tuple[str, ...] = (
    "why_move",       # 为什么涨/跌 -- the move, the window, what explains it
    "what_happened",  # 发生了什么 / 有什么新闻
    "compare",        # 和同业比
    "view",           # 怎么看 -- a house view against the market's
    "debate",         # 市场在争什么
    "valuation",      # 贵不贵
    "risk",           # 下行风险
    "forecast",       # 指引 / 预测 / 下季度
    "event_impact",   # X 对 Y 有什么影响
    "catalyst",       # 下次什么时候
    "fact",           # 多少
    "other",
)

# The kinds for which an answer must state where we differ from the market.
VIEW_KINDS: frozenset[str] = frozenset({"view", "debate"})
# The kinds whose retrieval is anchored on a date window and its recency.
DATED_KINDS: frozenset[str] = frozenset({"why_move", "what_happened"})

# Ordered: the first pattern that matches wins.
_KIND_PATTERNS: tuple[tuple[str, str], ...] = (
    ("why_move", r"为什么(?:会)?(?:大)?(?:涨|跌|拉升|跳水|暴涨|暴跌|上涨|下跌|走强|走弱|异动)|"
                 r"(?:大涨|大跌|暴涨|暴跌|拉升|跳水|异动|涨停|跌停)(?:的)?(?:原因|是因为|因为什么)|"
                 r"(?:涨|跌)(?:了)?(?:这么多|那么多|这么猛)|股价.{0,12}(?:为什么|为何|原因)|"
                 r"why (?:did|is|was|has) .{0,40}(?:up|down|rally|rallied|drop|dropped|fall|fell|jump|"
                 r"jumped|surge|surged|move|moved|sell ?off|tank)|what (?:drove|moved|caused) the"),
    ("what_happened", r"发生了什么|出了什么事|怎么了|有什么(?:新|最新)?(?:消息|新闻|动态|变化)|最新(?:消息|动态|进展)|"
                      r"what happened|any news|what'?s new|latest news"),
    ("compare", r"对比|相比|比较|比起|和.{1,12}比|跟.{1,12}比|versus|\bvs\.?\b|compared? (?:to|with)|relative to peers"),
    ("view", r"怎么看|看法|观点|看多|看空|该不该|值不值得|要不要买|"
             r"what do you think|your view|our view|bull(ish)?|bear(ish)?|"
             r"should we (buy|own|sell)|do you like"),
    ("debate", r"争议|分歧|在吵|多空|市场担心|bear case|bull case|debate|"
               r"what is the market (arguing|worried)|controvers"),
    ("valuation", r"估值|贵不贵|便宜|多少倍|市盈|倍数|分位|目标价|valuation|expensive|cheap|"
                  r"multiple|p/?e\b|ev/ebitda|fcf yield|percentile|price target"),
    ("risk", r"风险|下行|隐患|担忧|利空|证伪|risk|downside|headwind"),
    ("forecast", r"预测|预计|预期|下季度|下个季度|明年|未来|会不会|将会|指引|展望|"
                 r"forecast|outlook|guidance|estimate|next (quarter|year)|"
                 r"do you expect|going to"),
    ("event_impact", r"影响|意味着|冲击|利好|impact|implication|mean for|"
                     r"affect|read.?through"),
    ("catalyst", r"什么时候|何时|下一次|下次|日程|财报日|when (is|does|will)|next "
                 r"(earnings|print|catalyst|report)|calendar"),
    ("fact", r"多少|几个|是多少|数字|怎么样|如何|利用率|利润率|人员流失|"
             r"how much|how many|what was|what were|"
             r"revenue|margin|growth|bookings|headcount"),
)


def question_kind(question: str) -> dict[str, Any]:
    """What this question is about, and which words said so."""

    lowered = (question or "").lower()
    for kind, pattern in _KIND_PATTERNS:
        match = re.search(pattern, lowered)
        if match is not None:
            return {"kind": kind, "matched": match.group(0)}
    return {"kind": "other", "matched": None}


# ---------------------------------------------------------------------------
# subjects
# ---------------------------------------------------------------------------

def resolve_subjects(
    question: str,
    members: Mapping[str, Mapping[str, Any]],
    *,
    company_names: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Which companies (and whether the industry) the question is about."""

    names = dict(company_names or {})
    lowered = (question or "").lower()
    words = set(re.findall(r"[a-z]+", lowered))
    named: list[str] = []
    for ref, member in members.items():
        ticker = str(member.get("ticker") or "")
        name = names.get(ticker, "") or str(member.get("name") or "")
        if (ticker and ticker.lower() in words) or (name and name.lower() in lowered):
            named.append(ref)
    industry = bool(re.search(r"行业|同业|板块|industry|sector|peers|竞争对手", lowered))
    return {
        "companies": sorted(named),
        "scope": "named" if named else "universe",
        "industry": industry or not named,
        "all_companies": sorted(members),
    }


# ---------------------------------------------------------------------------
# relative dates
# ---------------------------------------------------------------------------

def _day(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _previous_trading_day(day: date, trading_days: Sequence[str], *, steps: int = 1) -> tuple[date, str]:
    """The ``steps``-th trading day strictly before ``day``, and how it was known."""

    held = sorted({d for d in trading_days if _day(d) is not None and d < day.isoformat()})
    if len(held) >= steps:
        return date.fromisoformat(held[-steps]), "trading_calendar"
    cursor, left = day, steps
    while left:
        cursor -= timedelta(days=1)
        if cursor.weekday() < 5:
            left -= 1
    return cursor, "weekdays"


def _latest_trading_day(day: date, trading_days: Sequence[str]) -> tuple[date, str]:
    held = sorted(d for d in trading_days if _day(d) is not None and d <= day.isoformat())
    if held:
        return date.fromisoformat(held[-1]), "trading_calendar"
    cursor = day
    while cursor.weekday() >= 5:
        cursor -= timedelta(days=1)
    return cursor, "weekdays"


def _window(phrase: str, label: str, basis: str, start: date, end: date, *,
            anchor: date | None = None) -> dict[str, Any]:
    return {"resolved": True, "phrase": phrase, "label": label, "basis": basis,
            "anchor": None if anchor is None else anchor.isoformat(),
            "date_from": start.isoformat(), "date_to": end.isoformat(),
            "single_day": anchor is not None}


_EXPLICIT_DATE_RE = re.compile(r"(?<!\d)(20\d\d)[-/.年](\d{1,2})[-/.月](\d{1,2})日?")
_MONTH_DAY_RE = re.compile(r"(?<!\d)(1[0-2]|0?[1-9])\s*月\s*([12]\d|3[01]|0?[1-9])\s*[日号]")
_SLASH_DAY_RE = re.compile(r"(?<![\d/.\-])(1[0-2]|0?[1-9])[/-]([12]\d|3[01]|0?[1-9])(?![\d/\-])")
_LAST_N_RE = re.compile(r"(?:近|过去|最近|前)\s*(\d{1,3})\s*(?:个)?(?:交易日|天|日)|(?:last|past)\s+(\d{1,3})\s+"
                        r"(?:trading\s+)?days")


def resolve_dates(question: str, *, today: str, trading_days: Sequence[str] = (),
                  kind: str | None = None) -> dict[str, Any]:
    """The date or window the question means, resolved against the trading calendar.

    ``trading_days`` are the bar dates of the company's stored price series;
    without them a weekday calendar is used and ``basis`` says so.  A question
    with no date words gets its kind's default window (``resolved`` false).
    """

    now = _day(today) or date.today()
    q = (question or "").lower()

    def explicit(year: int, month: int, day_of_month: int) -> date | None:
        try:
            return date(year, month, day_of_month)
        except ValueError:
            return None

    match = _EXPLICIT_DATE_RE.search(q)
    if match:
        found = explicit(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if found is not None:
            return _window(match.group(0), found.isoformat(), "explicit", found, found, anchor=found)
    match = _MONTH_DAY_RE.search(q) or (_SLASH_DAY_RE.search(q) if kind in DATED_KINDS else None)
    if match:
        month, day_of_month = int(match.group(1)), int(match.group(2))
        found = explicit(now.year, month, day_of_month)
        if found is not None and found > now:
            found = explicit(now.year - 1, month, day_of_month)
        if found is not None:
            return _window(match.group(0), found.isoformat(), "explicit", found, found, anchor=found)

    rules: tuple[tuple[str, str], ...] = (
        ("day_before_yesterday", r"前天|前日|day before yesterday"),
        ("yesterday", r"昨天|昨日|yesterday|上个交易日|上一个交易日|previous (?:trading )?(?:day|session)"),
        ("today", r"今天|今日|today|盘中|this session"),
        ("last_week", r"上周|上星期|上个星期|上个礼拜|last week"),
        ("this_week", r"本周|这周|这个星期|这个礼拜|this week"),
        ("last_month", r"上个月|上月|last month"),
        ("this_month", r"本月|这个月|this month"),
        ("past_week", r"最近一周|近一周|过去一周|这一周|past week|last 7 days"),
        ("past_month", r"最近一个月|近一个月|过去一个月|近30天|past month"),
        ("ytd", r"今年以来|年初至今|今年|ytd|year to date|this year"),
    )
    for name, pattern in rules:
        found = re.search(pattern, q)
        if found is None:
            continue
        phrase = found.group(0)
        if name == "day_before_yesterday":
            day, basis = _previous_trading_day(now, trading_days, steps=2)
            return _window(phrase, f"前一个交易日之前的交易日 {day.isoformat()}", basis, day, day, anchor=day)
        if name == "yesterday":
            day, basis = _previous_trading_day(now, trading_days)
            return _window(phrase, f"上一个交易日 {day.isoformat()}", basis, day, day, anchor=day)
        if name == "today":
            return _window(phrase, f"今天 {now.isoformat()}", "calendar", now, now, anchor=now)
        if name == "last_week":
            monday = now - timedelta(days=now.weekday() + 7)
            return _window(phrase, f"上周 {monday.isoformat()}–{(monday + timedelta(days=6)).isoformat()}",
                           "calendar", monday, monday + timedelta(days=6))
        if name == "this_week":
            monday = now - timedelta(days=now.weekday())
            return _window(phrase, f"本周 {monday.isoformat()}–{now.isoformat()}", "calendar", monday, now)
        if name == "last_month":
            first_this = now.replace(day=1)
            last_prev = first_this - timedelta(days=1)
            return _window(phrase, f"上个月 {last_prev.replace(day=1).isoformat()}–{last_prev.isoformat()}",
                           "calendar", last_prev.replace(day=1), last_prev)
        if name == "this_month":
            return _window(phrase, f"本月 {now.replace(day=1).isoformat()}–{now.isoformat()}", "calendar",
                           now.replace(day=1), now)
        if name == "past_week":
            return _window(phrase, f"最近 7 天 {(now - timedelta(days=7)).isoformat()}–{now.isoformat()}",
                           "calendar", now - timedelta(days=7), now)
        if name == "past_month":
            return _window(phrase, f"最近 30 天 {(now - timedelta(days=30)).isoformat()}–{now.isoformat()}",
                           "calendar", now - timedelta(days=30), now)
        if name == "ytd":
            start = date(now.year, 1, 1)
            return _window(phrase, f"今年以来 {start.isoformat()}–{now.isoformat()}", "calendar", start, now)
    found = _LAST_N_RE.search(q)
    if found:
        days = max(1, min(int(found.group(1) or found.group(2)), 730))
        return _window(found.group(0), f"最近 {days} 天", "calendar", now - timedelta(days=days), now)
    found = re.search(r"最近|近期|近来|这两天|这几天|recently|lately", q)
    if found:
        return _window(found.group(0), f"最近 14 天 {(now - timedelta(days=14)).isoformat()}–{now.isoformat()}",
                       "calendar", now - timedelta(days=14), now)
    # No date words: the kind's own default.
    if kind == "why_move":
        day, basis = _latest_trading_day(now, trading_days)
        return {**_window("", f"最近一个交易日 {day.isoformat()}", basis, day, day, anchor=day), "resolved": False}
    if kind == "what_happened":
        return {**_window("", f"最近 14 天 {(now - timedelta(days=14)).isoformat()}–{now.isoformat()}",
                          "calendar", now - timedelta(days=14), now), "resolved": False}
    return {**_window("", f"最近一年 {(now - timedelta(days=365)).isoformat()}–{now.isoformat()}",
                      "calendar", now - timedelta(days=365), now), "resolved": False}


# ---------------------------------------------------------------------------
# retrieval profiles
# ---------------------------------------------------------------------------

FIRST_HAND_KINDS: frozenset[str] = frozenset({"sec_exhibit", "sec_8k", "sec_10k", "sec_10q", "sec_other",
                                              "transcript"})
STREET_KINDS: frozenset[str] = frozenset({"broker_report", "sales_note"})
OUR_OWN_KINDS: frozenset[str] = frozenset({"prior_research", "company_wiki"})

_BASE_WEIGHTS: Mapping[str, float] = {
    "sec_exhibit": 1.0, "transcript": 1.0, "sec_8k": 0.9, "sec_10k": 0.9, "sec_10q": 0.9, "sec_other": 0.7,
    "broker_report": 0.9, "sales_note": 0.75, "expert_call": 0.7, "web_page": 0.55, "research_other": 0.6,
    # Our own earlier research and the house wiki are context, not evidence.
    "prior_research": 0.35, "company_wiki": 0.35,
}


def _weights(**overrides: float) -> dict[str, float]:
    return {**_BASE_WEIGHTS, **overrides}


#: Per question type: kind weights, recency, how far back the context window
#: reaches, and the vocabulary that type of question is answered with.
PROFILES: Mapping[str, Mapping[str, Any]] = {
    "why_move": {"weights": _weights(sales_note=1.0, broker_report=0.95, web_page=0.65, expert_call=0.4),
                 "half_life_days": 7.0, "lookback_days": 14, "window_bonus": 1.6,
                 "facets": ("shares stock rose fell rally move investors reaction upgrade downgrade",
                            "results earnings revenue bookings guidance outlook beat",
                            "preview expectations consensus implied move positioning")},
    "what_happened": {"weights": _weights(web_page=0.7, sales_note=0.85, expert_call=0.5),
                      "half_life_days": 10.0, "lookback_days": 7, "window_bonus": 1.5,
                      "facets": ("announced results news agreement acquisition leadership",
                                 "results earnings revenue bookings guidance outlook")},
    "forecast": {"weights": _weights(transcript=1.0, sec_exhibit=1.0, broker_report=1.0, sales_note=0.6),
                 "half_life_days": 120.0, "lookback_days": 365, "window_bonus": 1.0,
                 "facets": ("guidance outlook expects fiscal growth range", "estimates forecast EPS revenue consensus")},
    "valuation": {"weights": _weights(broker_report=1.0, sales_note=0.75, transcript=0.6, sec_exhibit=0.6,
                                      sec_8k=0.5, sec_10k=0.5, sec_10q=0.5),
                  "half_life_days": 120.0, "lookback_days": 365, "window_bonus": 1.0,
                  "facets": ("valuation multiple P/E price target upside downside rating",)},
    "risk": {"weights": _weights(broker_report=1.0, transcript=0.9, sec_10k=0.95, sec_10q=0.9, expert_call=0.85),
             "half_life_days": 180.0, "lookback_days": 365, "window_bonus": 1.0,
             "facets": ("risk headwind pressure downside decline weakness", "risk factors competition AI disruption")},
    "compare": {"weights": _weights(broker_report=1.0, transcript=0.9, expert_call=0.8),
                "half_life_days": 180.0, "lookback_days": 365, "window_bonus": 1.0,
                "facets": ("growth margin share competition peers versus",)},
}
_DEFAULT_PROFILE: Mapping[str, Any] = {"weights": _weights(), "half_life_days": 180.0, "lookback_days": 365,
                                       "window_bonus": 1.0, "facets": ()}

#: Chinese finance words the corpus (mostly English) is written in.
TERM_GLOSSARY: Mapping[str, str] = {
    "收入": "revenue", "营收": "revenue", "订单": "bookings", "新签": "bookings", "指引": "guidance",
    "展望": "outlook", "利润率": "margin", "毛利率": "gross margin", "营业利润": "operating income",
    "每股收益": "EPS", "回购": "buyback repurchase", "分红": "dividend", "估值": "valuation",
    "目标价": "price target", "评级": "rating upgrade downgrade", "上调": "upgrade raise",
    "下调": "downgrade cut", "需求": "demand", "裁员": "layoffs headcount", "人员": "headcount",
    "利用率": "utilization", "并购": "acquisition", "收购": "acquisition", "自由现金流": "free cash flow",
    "现金流": "cash flow", "咨询": "consulting", "托管": "managed services", "外包": "outsourcing",
    "人工智能": "AI generative", "生成式": "generative AI", "定价": "pricing", "竞争": "competition",
    "份额": "share", "管理层": "management CEO", "业绩": "results earnings", "财报": "earnings results",
    "季度": "quarter", "一致预期": "consensus", "预期": "expectations", "汇率": "currency FX",
    "本币": "local currency", "增速": "growth", "风险": "risk", "关税": "tariff", "政府": "federal government",
    "股价": "shares stock", "大涨": "rally surged", "大跌": "fell plunged", "涨": "rose", "跌": "fell",
}

_QUESTION_NOISE_RE = re.compile(
    r"关于|为什么|为何|怎么看|怎么|什么|如何|是否|有没有|多少|一下|请问|呢|吗|吧|的|了|和|与|及|在|是|对|"
    r"昨天|昨日|前天|今天|今日|上周|本周|这周|上个月|本月|最近|近期|股价|大涨|大跌|涨|跌|"
    r"[0-9]+月[0-9]+[日号]|20\d\d-\d\d-\d\d")


def query_terms(question: str, *, names: Sequence[str] = ()) -> list[str]:
    """The question's own content words, English glossary forms added; company names removed."""

    text = question or ""
    for name in names:
        if name:
            text = re.sub(re.escape(name), " ", text, flags=re.IGNORECASE)
    terms: list[str] = []
    for zh, en in TERM_GLOSSARY.items():
        if zh in text:
            terms.extend(en.split())
    stripped = _QUESTION_NOISE_RE.sub(" ", text)
    for word in re.findall(r"[A-Za-z][A-Za-z0-9&.\-]{1,30}|[一-鿿]{2,8}", stripped):
        if word.lower() in {"acn", "the", "and", "why", "what", "did", "does", "stock", "shares"}:
            continue
        terms.append(word)
    seen: set[str] = set()
    out = []
    for term in terms:
        key = term.lower()
        if key not in seen:
            seen.add(key)
            out.append(term)
    return out[:16]


def _profile(kind: str) -> Mapping[str, Any]:
    return PROFILES.get(kind, _DEFAULT_PROFILE)


def _recency(document_date: Any, reference: date, half_life: float, *, floor: float = 0.2) -> float:
    day = _day(document_date)
    if day is None:
        return 0.5
    age = abs((reference - day).days)
    return floor + (1 - floor) * math.pow(0.5, age / max(half_life, 0.5))


def _mentions(item: Mapping[str, Any], terms: Sequence[str]) -> bool:
    text = f"{item.get('title') or ''} {item.get('text') or ''}"
    for term in terms:
        if term and re.search(rf"(?<![A-Za-z]){re.escape(term)}(?![A-Za-z])", text, re.IGNORECASE):
            return True
    return False


def retrieval_windows(kind: str, dates: Mapping[str, Any], trading_days: Sequence[str]) -> dict[str, Any]:
    """The asked-about window and the wider context window retrieval reads.

    A single day widens to the previous trading day as well: a move on day D
    is explained in notes written on D-1 evening and D's pre-market.
    """

    profile = _profile(kind)
    start = _day(dates.get("date_from")) or date.today()
    end = _day(dates.get("date_to")) or start
    if dates.get("single_day"):
        start = _previous_trading_day(end, trading_days)[0]
    lookback = int(profile.get("lookback_days") or 365)
    context_from = min(start, end - timedelta(days=lookback))
    return {"primary": {"date_from": start.isoformat(), "date_to": end.isoformat()},
            "context": {"date_from": context_from.isoformat(), "date_to": end.isoformat()}}


def retrieve(
    state_dir: str | Path | None,
    *,
    question: str,
    kind: str,
    company_ref: str | None,
    company_terms: Sequence[str] = (),
    windows: Mapping[str, Any],
    limit: int = 24,
    search: Callable[..., Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Passages for one company (or the whole corpus), reranked for this question.

    Several focused searches -- the question's own terms, then each facet of
    the question type -- inside the asked-about window and the context window;
    merged by passage and rescored:

        relevance x (0.35 + 0.65 x coverage) x kind weight(type) x recency(type)
        x window bonus (inside the asked-about window) x first-party bonus

    Returns ``{"status", "results", "queries", "candidates"}``; ``status`` is
    the evidence index's own (``no_index`` when this environment has none).
    """

    from . import evidence_index

    run = search or (lambda q, **kw: evidence_index.evidence_search(state_dir, q, **kw))
    profile = _profile(kind)
    weights = profile["weights"]
    terms = query_terms(question, names=company_terms)
    head = " ".join(dict.fromkeys(t for t in company_terms if t))
    queries: list[tuple[str, str]] = []
    if terms:
        queries.append(("question", " ".join([head, *terms]).strip()))
    for facet in profile.get("facets") or ():
        queries.append(("facet", f"{head} {facet} {' '.join(terms[:4])}".strip()))
    if not queries:
        queries.append(("company", head or question))
    primary = windows["primary"]
    context = windows["context"]
    spans = [("primary", primary)]
    if context != primary:
        spans.append(("context", context))
    reference = _day(primary["date_to"]) or date.today()
    in_primary = (primary["date_from"], primary["date_to"])
    status = "empty"
    merged: dict[str, dict[str, Any]] = {}
    ran: list[dict[str, Any]] = []
    for label, query in queries:
        for span_name, span in spans:
            try:
                found = run(query[:390], company_ref=company_ref, date_from=span["date_from"],
                            date_to=span["date_to"], limit=30, per_document=2)
            except Exception as exc:  # noqa: BLE001 - one bad search is an empty one
                ran.append({"query": query[:120], "window": span_name, "status": "error",
                            "reason": f"{type(exc).__name__}"})
                continue
            ran.append({"query": query[:120], "window": span_name, "status": found.get("status"),
                        "hits": len(found.get("results") or ())})
            if found.get("status") == "no_index":
                status = "no_index"
                continue
            if found.get("status") == "ok":
                status = "ok"
            for item in found.get("results") or ():
                parts = item.get("score_parts") or {}
                relevance = float(parts.get("relevance") or 0.0) * (0.35 + 0.65 * float(parts.get("coverage") or 0))
                doc_kind = str(item.get("kind") or "")
                weight = float(weights.get(doc_kind, 0.6))
                recency = _recency(item.get("date"), reference, float(profile["half_life_days"]),
                                   floor=0.2 if kind in DATED_KINDS else 0.35)
                inside = in_primary[0] <= str(item.get("date") or "")[:10] <= in_primary[1]
                bonus = float(profile.get("window_bonus") or 1.0) if inside else 1.0
                own = 1.0 if (company_ref is None or company_ref in (item.get("filed_company_refs") or ())) else 0.85
                score = relevance * weight * recency * bonus * own
                if company_terms and doc_kind not in FIRST_HAND_KINDS and not _mentions(item, company_terms):
                    # Filed under the company by a discovery, but the passage
                    # never names it (an expert call on another industry).
                    continue
                ref = item["passage_ref"]
                held = merged.get(ref)
                if held is None or held["rank_score"] < score:
                    merged[ref] = {**dict(item), "rank_score": round(score, 6), "in_window": inside,
                                   "first_hand": doc_kind in FIRST_HAND_KINDS,
                                   "street": doc_kind in STREET_KINDS,
                                   "our_own": doc_kind in OUR_OWN_KINDS,
                                   "matched_by": label}
    ranked = sorted(merged.values(), key=lambda row: (-row["rank_score"], row["passage_ref"]))
    # At most three passages of one document; a 50K-character call must not
    # crowd out the filing and the note that explain it.
    per_doc: dict[str, int] = {}
    chosen: list[dict[str, Any]] = []
    for row in ranked:
        count = per_doc.get(row["document_ref"], 0)
        if count >= 3:
            continue
        per_doc[row["document_ref"]] = count + 1
        chosen.append(row)
        if len(chosen) >= limit:
            break
    return {"status": status if merged or status == "no_index" else "empty", "results": chosen,
            "queries": ran, "candidates": len(merged), "terms": terms}


# ---------------------------------------------------------------------------
# blocks
# ---------------------------------------------------------------------------

BLOCK_LABELS: Mapping[str, str] = {
    "move": "股价表现（由价格序列计算，含同业与基准对照）",
    "events": "这段时间发生的事（股价异动、新文件、日程）",
    "passages": "与问题相关的原文段落（一手材料、券商研报与销售快评）",
    "numbers": "数字包（一致预期、券商预测、业绩稿指引、估值、行情）",
    "house_view": "我们的看法（非证据：只能写进「我们的看法」，不能当作发生了什么的出处）",
    "claims": "旧结论（只在没有原文段落时提供）",
}
BLOCK_TAGS: Mapping[str, str] = {
    "move": "M", "events": "E", "passages": "P", "numbers": "N", "house_view": "O", "claims": "C",
}
#: The ask v0.1 tables (P15a), frozen: an answer published before batch G was
#: written against these tags, and recovering one from its stored prompt
#: (``scripts/recover_failed_cockpit_ask.py``) has to read them as they were.
LEGACY_BLOCK_LABELS: Mapping[str, str] = {
    "theses": "我们已经立下的论点", "dossier": "公司档案（按主题分节）", "debates": "还在争的问题",
    "claims": "账本里的结论", "forecast": "我们的模型：驱动因素、假设与已实现", "valuation": "估值快照",
    "market_price": "最新股价", "consensus": "市场一致预期", "catalyst": "下一个日程",
    "events": "最近发生的事", "judgements": "我们对这些事的判断", "reflections": "事后反思",
    "journal": "你上次对答案的反馈", "refreshed": "刚补搜到的文件（只有标题，正文还没读）",
}
LEGACY_BLOCK_TAGS: Mapping[str, str] = {
    "theses": "T", "dossier": "D", "debates": "B", "claims": "C", "forecast": "F", "valuation": "V",
    "market_price": "P", "consensus": "S", "catalyst": "K", "events": "E", "judgements": "G",
    "reflections": "R", "journal": "N", "refreshed": "H",
}
BLOCK_ORDER: tuple[str, ...] = ("move", "events", "passages", "numbers", "house_view", "claims")
#: Kept as a name for older readers: the order is fixed, nothing is promoted.
BLOCK_PRIORITY = BLOCK_ORDER
EVIDENCE_BLOCKS: frozenset[str] = frozenset({"move", "events", "passages", "numbers", "claims"})

UNAVAILABLE_REASONS: tuple[str, ...] = (
    "no_authority_on_this_core",
    "no_record_for_this_company",
    "no_company_resolved",
    "reader_not_available",
    "dropped_for_budget",
    "no_index",
    "nothing_in_window",
    "passages_found",
)

ROW_CAPS: Mapping[str, int] = {"move": 4, "events": 12, "passages": 28, "numbers": 40, "house_view": 6,
                               "claims": MAX_CLAIM_ROWS}
MAX_ROW_CHARS = 700
MAX_PASSAGE_CHARS = 1600


class AskContextError(RuntimeError):
    """The assembler was asked for something it cannot build."""


def _table_exists(connection: Any, name: str) -> bool:
    if connection is None:
        return False
    try:
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None
    except Exception:  # noqa: BLE001
        return False


def _trim(text: Any, limit: int = MAX_ROW_CHARS) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _block(name: str, rows: Sequence[Mapping[str, Any]], *, reason: str | None = None,
           note: str | None = None) -> dict[str, Any]:
    if reason is not None and reason not in UNAVAILABLE_REASONS:
        raise AskContextError(f"{reason!r} is not a reason a block can be absent")
    kept = [dict(row) for row in rows][: ROW_CAPS.get(name, 12)]
    return {"block": name, "label": BLOCK_LABELS[name], "tag_letter": BLOCK_TAGS[name],
            "available": bool(kept), "reason": None if kept else (reason or "no_record_for_this_company"),
            "note": note, "rows": kept}


def _pct(value: Any, digits: int = 2) -> str | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return f"{number:+.{digits}f}"


# -- prices -----------------------------------------------------------------

def _latest_series(core: Any, company_ref: str) -> dict[str, Any] | None:
    if not _table_exists(core, "market_price_series_versions"):
        return None
    row = core.execute(
        "SELECT version_id, record_json FROM market_price_series_versions WHERE company_ref=? "
        "ORDER BY version_number DESC LIMIT 1", (company_ref,)).fetchone()
    if row is None:
        return None
    record = json.loads(row["record_json"])
    record.setdefault("id", row["version_id"])
    return record


def _closes(series: Mapping[str, Any] | None, until: str) -> list[tuple[str, float]]:
    out = []
    for bar in (series or {}).get("bars") or ():
        try:
            close = float(bar.get("close"))
        except (TypeError, ValueError):
            continue
        if str(bar.get("date")) <= until:
            out.append((str(bar["date"]), close))
    out.sort()
    return out


def _move(closes: Sequence[tuple[str, float]], start: str, end: str) -> dict[str, Any] | None:
    """Close-to-close change from the last close before ``start`` to the last close on/before ``end``."""

    before = [item for item in closes if item[0] < start]
    upto = [item for item in closes if item[0] <= end]
    if not before or not upto or upto[-1][0] < start:
        return None
    base, last = before[-1], upto[-1]
    if not base[1]:
        return None
    return {"from_date": base[0], "from_close": base[1], "to_date": last[0], "to_close": last[1],
            "percent": (last[1] / base[1] - 1) * 100}


def _fmt_close(value: float) -> str:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text


def move_block(core: Any, *, companies: Sequence[str], universe: Sequence[str], dates: Mapping[str, Any],
               windows: Mapping[str, Any], kind: str, label: Callable[[str], str], today: str) -> dict[str, Any]:
    """The asked-about move (and the biggest moves of the lookback), with peers and a benchmark."""

    from .market_data_block import BENCHMARK_TICKERS

    if not _table_exists(core, "market_price_series_versions"):
        return _block("move", (), reason="no_authority_on_this_core")
    if not companies:
        return _block("move", (), reason="no_company_resolved")
    until = min(str(today), str(dates.get("date_to") or today))
    benchmark = None
    try:
        held = {row["ticker"]: row["company_ref"] for row in core.execute(
            "SELECT DISTINCT ticker, company_ref FROM market_price_series_versions")}
        ticker = next((t for t in BENCHMARK_TICKERS if t in held), None)
        if ticker is not None:
            benchmark = (ticker, _closes(_latest_series(core, held[ticker]), until))
    except Exception:  # noqa: BLE001 - no benchmark is said plainly below
        benchmark = None
    rows: list[dict[str, Any]] = []
    peer_series = {ref: _closes(_latest_series(core, ref), until) for ref in universe}
    for company in companies[:3]:
        series = _latest_series(core, company)
        closes = _closes(series, until)
        if not closes:
            continue
        start = str(dates.get("date_from") or until)
        end = str(dates.get("date_to") or until)
        spans = [(start, end, "asked")]
        if kind in DATED_KINDS or kind == "event_impact":
            # The biggest single-day moves of the context window, beyond the asked day.
            context_from = str(windows["context"]["date_from"])
            days = [(closes[i][0], (closes[i][1] / closes[i - 1][1] - 1) * 100)
                    for i in range(1, len(closes)) if context_from <= closes[i][0] <= end
                    and not (start <= closes[i][0] <= end) and closes[i - 1][1]]
            for day, _ in sorted(days, key=lambda item: -abs(item[1]))[:2]:
                if abs(_) >= 2.0:
                    spans.append((day, day, "context"))
        for span_start, span_end, role in spans:
            own = _move(closes, span_start, span_end)
            if own is None:
                continue
            peers = []
            for ref in universe:
                if ref == company:
                    continue
                theirs = _move(peer_series.get(ref) or (), span_start, span_end)
                if theirs is not None and theirs["to_date"] == own["to_date"] and theirs["from_date"] == own["from_date"]:
                    peers.append({"company": label(ref), "percent": round(theirs["percent"], 2),
                                  "_raw": theirs["percent"]})
            basket = (sum(p.pop("_raw") for p in peers) / len(peers)) if peers else None
            bench = None
            if benchmark is not None:
                theirs = _move(benchmark[1], span_start, span_end)
                if (theirs is not None and theirs["from_date"] == own["from_date"]
                        and theirs["to_date"] == own["to_date"]):
                    bench = {"ticker": benchmark[0], "percent": round(theirs["percent"], 2)}
            single = own["from_date"] != own["to_date"] and span_start == span_end
            head = (f"{label(company)} {own['to_date']} 收盘 {_fmt_close(own['to_close'])}（前一交易日 "
                    f"{own['from_date']} 收盘 {_fmt_close(own['from_close'])}），单日 {_pct(own['percent'])}%"
                    if single else
                    f"{label(company)} {own['from_date']} 收盘 {_fmt_close(own['from_close'])} → {own['to_date']} 收盘 "
                    f"{_fmt_close(own['to_close'])}，区间 {_pct(own['percent'])}%")
            parts = [head]
            if basket is not None:
                parts.append(f"同期覆盖同业等权 basket（{'、'.join(p['company'] for p in peers)}）{_pct(basket)}%，"
                             f"超额 {_pct(own['percent'] - basket)} 个百分点")
            else:
                parts.append("同期没有可比的覆盖同业价格")
            parts.append(f"宽基基准 {bench['ticker']} {_pct(bench['percent'])}%" if bench else
                         "宽基基准不可得（未取得相同起止交易日的基准行情）" if benchmark is not None else
                         "宽基基准不可得（价格库里没有 SPY 等宽基指数序列）")
            text = "；".join(parts)
            if role == "context":
                text = "（窗口内另一次大幅波动）" + text
            rows.append({
                "ref": (series or {}).get("id") or f"market-price-series:{company}",
                "text": text,
                "period": own["to_date"],
                "detail": {"company": label(company), "role": role, "from_date": own["from_date"],
                           "to_date": own["to_date"], "percent": round(own["percent"], 2),
                           "basket_percent": None if basket is None else round(basket, 2),
                           "excess_pp": None if basket is None else round(own["percent"] - basket, 2),
                           "peers": peers, "benchmark": bench,
                           "kind": "price_move", "first_hand": True},
            })
    note = ("「昨天」等相对日期按这家公司价格序列的交易日解析：" + str(dates.get("label") or "")
            + ("（价格序列不可用，按工作日推算）" if dates.get("basis") == "weekdays" else ""))
    return _block("move", rows, reason="no_record_for_this_company", note=note)


# -- events -----------------------------------------------------------------

def _doc_headers(state_dir: str | Path | None, refs: Sequence[str]) -> dict[str, dict[str, Any]]:
    """Title, kind, house and date of indexed documents, by ref (read-only)."""

    if state_dir is None or not refs:
        return {}
    from .evidence_index import open_index_readonly

    connection = open_index_readonly(state_dir)
    if connection is None:
        return {}
    try:
        marks = ",".join("?" for _ in refs)
        rows = connection.execute(
            f"SELECT document_ref, kind, title, house, document_date FROM evidence_documents "
            f"WHERE document_ref IN ({marks}) AND superseded_by IS NULL", list(refs)).fetchall()
        return {row["document_ref"]: dict(row) for row in rows}
    except Exception:  # noqa: BLE001
        return {}
    finally:
        connection.close()


_EVENT_KIND_LABELS: Mapping[str, str] = {
    "price_move": "股价异动", "news": "新闻", "sales_note": "卖方销售快报", "expert_excerpt": "专家访谈",
    "calendar": "日程", "filing": "公告", "sec_filing": "SEC 申报", "earnings_release": "业绩稿",
    "transcript": "电话会纪要", "broker_report": "卖方研报",
}


def event_row(event_id: str, kind: str, occurred_at: str, tier: Any, record: Mapping[str, Any], *,
              company: str, headers: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """One research event as an analyst reads it: what moved, or what was published."""

    from .evidence_corpus import KIND_LABELS

    payload = record.get("payload") or {}
    day = str(payload.get("as_of") or occurred_at or "")[:10]
    detail: dict[str, Any] = {"company": company, "kind": kind, "evidence_tier": tier, "occurred_at": occurred_at,
                              "source_refs": list(record.get("source_refs") or [])[:6]}
    if kind == "price_move":
        parts = [f"{day} 股价{'上涨' if str(payload.get('direction')) == 'up' else '下跌'} "
                 f"{_pct(payload.get('return_percent'))}%（收盘 {payload.get('close')}，前收 {payload.get('previous_close')}）"]
        if payload.get("basket_return_percent") is not None:
            parts.append(f"覆盖同业 basket {_pct(payload.get('basket_return_percent'))}%"
                         f"（{payload.get('basket_members')} 家），超额 {_pct(payload.get('excess_vs_basket_percent'))} 个百分点")
        if payload.get("benchmark_return_percent") is not None:
            parts.append(f"基准 {_pct(payload.get('benchmark_return_percent'))}%，超额 "
                         f"{_pct(payload.get('excess_vs_benchmark_percent'))} 个百分点")
        else:
            parts.append("宽基基准不可得")
        text = "；".join(parts)
        detail.update({"return_percent": payload.get("return_percent"),
                       "excess_vs_basket_percent": payload.get("excess_vs_basket_percent")})
    elif kind == "calendar":
        text = (f"日程：{payload.get('event_kind') or '事件'} {payload.get('expected_date') or day}"
                + ("（已确认）" if payload.get("confirmed") else "（推算，未确认）"))
    else:
        document_ref = payload.get("document_ref")
        header = headers.get(str(document_ref)) or {}
        title = (payload.get("title") or header.get("title") or payload.get("headline")
                 or payload.get("statement"))
        doc_kind = header.get("kind")
        kind_label = KIND_LABELS.get(doc_kind or "", _EVENT_KIND_LABELS.get(kind, kind))
        who = header.get("house") or payload.get("host")
        text = f"{day} {kind_label}" + (f" · {who}" if who else "")
        text += f"《{_trim(title, 160)}》" if title else "（标题未入索引）"
        if payload.get("spec_ref"):
            text += f"；检索主题 {payload['spec_ref']}"
        detail.update({"document_ref": document_ref, "title": title, "house": who, "doc_kind": doc_kind,
                       "document_date": header.get("document_date")})
    return {"ref": event_id, "text": _trim(text), "period": day, "detail": detail}


def events_block(core: Any, state_dir: str | Path | None, *, companies: Sequence[str], windows: Mapping[str, Any],
                 label: Callable[[str], str]) -> dict[str, Any]:
    if not _table_exists(core, "research_events"):
        return _block("events", (), reason="no_authority_on_this_core")
    if not companies:
        return _block("events", (), reason="no_company_resolved")
    marks = ",".join("?" for _ in companies)
    start = str(windows["context"]["date_from"])
    end = str(windows["context"]["date_to"])
    raw = core.execute(
        f"SELECT event_id, company_ref, kind, occurred_at, evidence_tier, record_json FROM research_events "
        f"WHERE company_ref IN ({marks}) AND substr(occurred_at,1,10) BETWEEN ? AND ? "
        # Extracted Claims are events too (hundreds per quarter); they are the
        # fallback block's business, never the tape.
        f"AND kind <> 'claim' "
        f"ORDER BY occurred_at DESC, event_id DESC LIMIT 200", (*companies, start, end)).fetchall()
    parsed = [(row, json.loads(row["record_json"])) for row in raw]
    refs = [str((record.get("payload") or {}).get("document_ref")) for _, record in parsed
            if (record.get("payload") or {}).get("document_ref")]
    headers = _doc_headers(state_dir, sorted(set(refs)))
    rows: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for row, record in parsed:
        payload = record.get("payload") or {}
        key = (row["company_ref"], row["kind"], payload.get("as_of") or payload.get("document_ref")
               or payload.get("expected_date") or row["occurred_at"][:10], payload.get("return_percent"))
        if key in seen:
            continue
        seen.add(key)
        rows.append(event_row(row["event_id"], row["kind"], row["occurred_at"], row["evidence_tier"], record,
                              company=label(row["company_ref"]), headers=headers))
    # Moves first (the tape), then documents newest first.
    rows.sort(key=lambda item: (item["detail"]["kind"] != "price_move", item["period"] or ""), reverse=False)
    moves = [item for item in rows if item["detail"]["kind"] == "price_move"]
    others = sorted((item for item in rows if item["detail"]["kind"] != "price_move"),
                    key=lambda item: item["period"] or "", reverse=True)
    moves.sort(key=lambda item: item["period"] or "", reverse=True)
    return _block("events", [*moves[:4], *others], reason="nothing_in_window")


# -- passages and numbers ----------------------------------------------------

def passage_row(item: Mapping[str, Any], *, label: Callable[[str], str], carried: bool = False) -> dict[str, Any]:
    from .evidence_corpus import KIND_LABELS

    kind = str(item.get("kind") or "")
    companies = [label(ref) for ref in item.get("filed_company_refs") or item.get("company_refs") or ()][:3]
    text = str(item.get("text") or "")
    if len(text) > MAX_PASSAGE_CHARS:
        text = text[:MAX_PASSAGE_CHARS].rstrip() + "…"
    return {
        "ref": item["passage_ref"],
        "text": " ".join(text.split()),
        "period": item.get("date"),
        "detail": {
            "company": "、".join(companies), "kind": kind, "kind_label": item.get("kind_label") or KIND_LABELS.get(kind, kind),
            "house": item.get("house"), "title": item.get("title"), "date": item.get("date"),
            "heading": item.get("heading"), "document_ref": item.get("document_ref"),
            "first_hand": kind in FIRST_HAND_KINDS, "street": kind in STREET_KINDS,
            "our_own": kind in OUR_OWN_KINDS, "in_window": bool(item.get("in_window")),
            "carried": carried, "rank_score": item.get("rank_score"),
            "source_text": str(item.get("text") or ""),
        },
    }


_NUMBER_TAG_RE = re.compile(r"\[N(\d+)\]\s*")
_NUMBER_SOURCES: tuple[tuple[str, str], ...] = (
    ("行情", "market"), ("涨跌", "market"), ("一致预期", "consensus"), ("券商研报预测", "street"),
    ("业绩稿", "outlook"), ("估值", "valuation"), ("我们的模型", "our_model"),
)


def number_rows(pack: Any, *, company: str) -> list[dict[str, Any]]:
    """The pack's numbers section, one row per line, each line's tags bound to its pointers."""

    text = str(getattr(pack, "text", "") or "")
    section = ""
    for chunk in re.split(r"\n(?=## \d+\. )", text):
        if chunk.startswith("## ") and "数字包" in chunk.split("\n", 1)[0]:
            section = chunk.split("\n", 1)[1] if "\n" in chunk else ""
            break
    rows: list[dict[str, Any]] = []
    heading_source = "numbers"
    heading_text = ""
    for line in section.split("\n"):
        raw = line.rstrip()
        if not raw.strip() or raw.startswith("（"):
            continue
        indented = raw.startswith("  ")
        if not indented:
            heading_source = next((source for prefix, source in _NUMBER_SOURCES if raw.startswith(prefix)), "numbers")
            heading_text = raw.split("：", 1)[0].split("（", 1)[0]
        tags = [f"N{number}" for number in _NUMBER_TAG_RE.findall(raw)]
        if not tags:
            continue
        clean = _NUMBER_TAG_RE.sub("", raw).strip()
        if indented and heading_text:
            clean = f"{heading_text} · {clean}"
        pointers = [pack.tags.get(tag) for tag in tags if pack.tags.get(tag)]
        rows.append({
            "ref": pointers[0] if pointers else f"material-pack:{pack.content_hash[:16]}",
            "text": _trim(clean, 600),
            "period": getattr(pack, "as_of", None),
            "detail": {"company": company, "source": heading_source, "pointers": pointers,
                       "pack_hash": getattr(pack, "content_hash", None), "kind": "numbers"},
        })
    return rows


def claim_rows(claims: Sequence[Mapping[str, Any]], *, companies: Sequence[str],
               label: Callable[[str], str]) -> list[dict[str, Any]]:
    pool = [c for c in claims if not companies or c.get("subject_ref") in set(companies)]
    pool = sorted(pool, key=lambda c: str(c.get("created_at") or ""), reverse=True)[:MAX_CLAIM_ROWS]
    return [{
        "ref": claim.get("ref"), "text": _trim(claim.get("statement"), 400), "period": claim.get("period"),
        "detail": {"company": label(str(claim.get("subject_ref") or "")), "at": str(claim.get("created_at") or "")[:10],
                   "kind": "claim"},
    } for claim in pool]


# -- our own view -------------------------------------------------------------

def _current_theses(theses: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    newest: dict[str, Mapping[str, Any]] = {}
    for item in theses:
        ref = str(item.get("thesis_ref") or item.get("id") or "")
        held = newest.get(ref)
        if held is None or str(item.get("created_at") or "") >= str(held.get("created_at") or ""):
            newest[ref] = item
    return list(newest.values())


def house_view_block(core: Any, *, theses: Sequence[Mapping[str, Any]], companies: Sequence[str],
                     members: Mapping[str, Mapping[str, Any]], label: Callable[[str], str],
                     industry: bool) -> dict[str, Any]:
    """Our current theses, dossier variant view and latest event judgements -- labelled as ours."""

    tickers = {str((members.get(ref) or {}).get("ticker") or "").lower(): ref for ref in companies}
    rows: list[dict[str, Any]] = []
    for thesis in _current_theses(theses):
        ref = str(thesis.get("thesis_ref") or thesis.get("id") or "")
        parts = ref.split(":")
        company = tickers.get(parts[1]) if len(parts) > 2 else None
        is_industry = len(parts) > 2 and parts[1] not in {str(m.get("ticker") or "").lower() for m in members.values()}
        if company is None and not (is_industry and (industry or companies)):
            continue
        statement = thesis.get("statement") or thesis.get("summary") or thesis.get("change_reason")
        if not statement:
            continue
        word = str(thesis.get("change_reason") or "").split("｜", 1)[0].strip()
        rows.append({
            "ref": thesis.get("id") or ref,
            "text": _trim(f"当前论点（{'行业' if company is None else label(company)}，信心 {thesis.get('confidence') or '未注明'}"
                          f"{'，最近一次修订：' + word if word.startswith('THESIS_') else ''}）：{statement}", 600),
            "period": str(thesis.get("created_at") or "")[:10],
            "detail": {"company": "" if company is None else label(company), "kind": "thesis",
                       "thesis_ref": ref, "confidence": thesis.get("confidence")},
        })
    if _table_exists(core, "company_dossier_versions") and companies:
        from .company_dossier import section_body

        for company in companies[:2]:
            row = core.execute("SELECT record_json FROM company_dossier_versions WHERE company_ref=? "
                               "ORDER BY version_number DESC LIMIT 1", (company,)).fetchone()
            if row is None:
                continue
            record = json.loads(row["record_json"])
            variant = record.get("variant_view") or {}
            if variant.get("status") == "drafted":
                rows.append({"ref": record.get("id"), "text": _trim("档案里的分歧观点：" + section_body(variant), 600),
                             "period": str(record.get("created_at") or "")[:10],
                             "detail": {"company": label(company), "kind": "dossier_variant"}})
    if _table_exists(core, "event_judgements") and companies:
        marks = ",".join("?" for _ in companies)
        for row in core.execute(
                f"SELECT judgement_id, company_ref, decision, action, created_at, record_json FROM event_judgements "
                f"WHERE company_ref IN ({marks}) ORDER BY created_at DESC LIMIT 2", tuple(companies)).fetchall():
            record = json.loads(row["record_json"])
            rows.append({"ref": row["judgement_id"],
                         "text": _trim(f"{str(row['created_at'])[:10]} 我们的事件研判：{row['decision']} / {row['action']} —— "
                                       f"{record.get('because') or ''}", 500),
                         "period": str(row["created_at"])[:10],
                         "detail": {"company": label(row["company_ref"]), "kind": "judgement",
                                    "decision": row["decision"]}})
    return _block("house_view", rows, reason="no_record_for_this_company",
                  note="以下是我们自己以前的判断，只作背景；回答「我们的看法」时以此为起点，说明这次的材料是否改变它。"
                       "不能把它当作「发生了什么」或「市场预期」的出处，也不能把「维持不变」本身当证据。")


# ---------------------------------------------------------------------------
# the assembler
# ---------------------------------------------------------------------------

def _trading_days(core: Any, companies: Sequence[str]) -> list[str]:
    for company in companies:
        series = _latest_series(core, company) if core is not None else None
        if series and series.get("bars"):
            return [str(bar.get("date")) for bar in series["bars"] if bar.get("date")]
    return []


def _company_terms(company: str, members: Mapping[str, Mapping[str, Any]], names: Mapping[str, str]) -> list[str]:
    member = members.get(company) or {}
    ticker = str(member.get("ticker") or "")
    name = names.get(ticker) or str(member.get("name") or "")
    short = re.sub(r"(?i)[,\s]+(plc|inc\.?|corp\.?|corporation|ltd\.?|holdings?|group|co\.?)\s*$", "", name).strip()
    return [term for term in dict.fromkeys([short, ticker]) if term]


def resolve_previous_passages(state_dir: str | Path | None, previous: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """The passages the previous answer cited, re-resolved (hash-checked) from the index."""

    if not previous or state_dir is None:
        return []
    from . import evidence_index

    out = []
    for ref in previous.get("passage_refs") or ():
        try:
            found = evidence_index.passage(state_dir, str(ref))
        except Exception:  # noqa: BLE001 - a ref that no longer resolves is not carried
            continue
        if found is not None:
            out.append(found)
    return out


def build_context(
    core: Any,
    *,
    question: str,
    mission: Mapping[str, Any],
    members: Mapping[str, Mapping[str, Any]],
    today: str,
    state_dir: str | Path | None = None,
    claims: Sequence[Mapping[str, Any]] = (),
    theses: Sequence[Mapping[str, Any]] = (),
    company_names: Mapping[str, str] | None = None,
    label: Callable[[str], str] | None = None,
    budget_chars: int = DEFAULT_BUDGET_CHARS,
    duplicates_dropped: int = 0,
    subject_hint: str = "",
    previous: Mapping[str, Any] | None = None,
    numbers_fn: Callable[..., Mapping[str, Any]] | None = None,
    search: Callable[..., Mapping[str, Any]] | None = None,
    consensus_reader: Any = None,  # accepted for older callers; the numbers pack carries consensus now
) -> dict[str, Any]:
    """Everything an analyst would have open for this question, tagged and bounded."""

    del consensus_reader
    if budget_chars < 2000:
        raise AskContextError("the context budget must leave room for the passages")
    naming = dict(company_names or {})
    show = label or (lambda ref: naming.get(str((members.get(ref) or {}).get("ticker") or ""), ref) or ref)
    kind = question_kind(question)
    subjects = resolve_subjects(question, members, company_names=naming)
    if not subjects["companies"] and subject_hint:
        hinted = resolve_subjects(subject_hint, members, company_names=naming)
        if hinted["companies"]:
            subjects = {**hinted, "scope": "follow_up"}
    if kind["kind"] == "other" and previous and previous.get("question_kind") in QUESTION_KINDS:
        kind = {"kind": str(previous["question_kind"]), "matched": "（沿用上一问的类型）"}
    named = list(subjects["companies"])
    universe = list(subjects["all_companies"])
    trading_days = _trading_days(core, named or universe)
    dates = resolve_dates(question, today=today, trading_days=trading_days, kind=kind["kind"])
    windows = retrieval_windows(kind["kind"], dates, trading_days)
    industry_ref = mission.get("industry_ref") if isinstance(mission.get("industry_ref"), str) else None

    # -- passages: per named company (compare: up to three), or the industry --
    targets: list[str | None] = named[:3] if named else [None]
    retrieval: dict[str, Any] = {"status": "empty", "per_subject": []}
    passages: list[dict[str, Any]] = []
    seen_refs: set[str] = set()
    carried = resolve_previous_passages(state_dir, previous)
    for item in carried:
        if item["passage_ref"] not in seen_refs:
            seen_refs.add(item["passage_ref"])
            passages.append({**item, "_carried": True})
    per_company_passages: dict[str | None, list[dict[str, Any]]] = {}
    if state_dir is not None:
        statuses = []
        for target in targets:
            terms = _company_terms(target, members, naming) if target else []
            found = retrieve(state_dir, question=question, kind=kind["kind"], company_ref=target,
                             company_terms=terms, windows=windows,
                             limit=ROW_CAPS["passages"] // max(1, len(targets)), search=search)
            statuses.append(found["status"])
            retrieval["per_subject"].append({"company": show(target) if target else "（行业）",
                                             "status": found["status"], "candidates": found["candidates"],
                                             "terms": found["terms"], "queries": found["queries"]})
            per_company_passages[target] = found["results"]
            for item in found["results"]:
                if item["passage_ref"] not in seen_refs:
                    seen_refs.add(item["passage_ref"])
                    passages.append(item)
        retrieval["status"] = ("ok" if "ok" in statuses else ("no_index" if "no_index" in statuses else "empty"))
    else:
        retrieval["status"] = "no_index"

    # -- the material pack: numbers, the passages rendered, claims fallback ----
    packs: list[dict[str, Any]] = []
    number_block_rows: list[dict[str, Any]] = []
    pack_claims_used = False
    if state_dir is not None:
        from . import material_pack

        for target in (named[:3] if named else ([industry_ref] if industry_ref else [])):
            try:
                pack = material_pack.build(
                    state_dir, target, "ask", today, topics=[question],
                    numbers_fn=numbers_fn, ranked_passages=per_company_passages.get(target, ()))
            except Exception as exc:  # noqa: BLE001 - no pack is an honest empty numbers block
                packs.append({"company_ref": target, "status": "failed",
                              "reason": f"{type(exc).__name__}: {str(exc)[:160]}"})
                continue
            packs.append({"company_ref": target, "status": "ok", "record": pack.record()})
            if target and not str(target).startswith("industry:"):
                number_block_rows += number_rows(pack, company=show(target))
            pack_claims_used = pack_claims_used or bool(
                (pack.budget.get("legacy_claims") or {}).get("items"))

    # -- blocks ------------------------------------------------------------------
    passage_rows = [passage_row(item, label=show, carried=bool(item.get("_carried"))) for item in passages]
    if retrieval["status"] == "no_index" and not passage_rows:
        passages_reason = "no_index"
    else:
        passages_reason = "nothing_in_window"
    blocks: dict[str, dict[str, Any]] = {
        "move": move_block(core, companies=named, universe=universe, dates=dates, windows=windows,
                           kind=kind["kind"], label=show, today=today),
        "events": events_block(core, state_dir, companies=named, windows=windows, label=show),
        "passages": _block("passages", passage_rows, reason=passages_reason,
                           note=(f"检索窗口：{windows['primary']['date_from']}–{windows['primary']['date_to']}"
                                 f"（问题所指），背景窗口 {windows['context']['date_from']} 起；"
                                 "按问题类型、时间远近与资料类型排序。一手材料 = 公司公告、新闻稿、电话会。")),
        "numbers": _block("numbers", number_block_rows,
                          reason=("no_company_resolved" if not named else "no_record_for_this_company")),
        "house_view": house_view_block(core, theses=theses, companies=named, members=members, label=show,
                                       industry=bool(subjects["industry"])),
        "claims": (_block("claims", (), reason="passages_found") if passage_rows else
                   _block("claims", claim_rows(claims, companies=named, label=show),
                          reason="no_record_for_this_company",
                          note="原文段落检索不到时才用：旧结论没有出处段落，数字只能在旧结论里逐字出现时引用。")),
    }

    # -- budget: passages are trimmed from the bottom of the ranking ------------
    def cost() -> int:
        return sum(len(render_block(block)) for block in blocks.values())

    dropped = 0
    while cost() > budget_chars and blocks["passages"]["rows"]:
        blocks["passages"]["rows"].pop()
        dropped += 1
    while cost() > budget_chars and blocks["claims"]["rows"]:
        blocks["claims"]["rows"].pop()
    blocks["passages"]["available"] = bool(blocks["passages"]["rows"])
    if not blocks["passages"]["rows"] and passage_rows:
        blocks["passages"]["reason"] = "dropped_for_budget"

    shown: list[dict[str, Any]] = []
    ordered: list[dict[str, Any]] = []
    for name in BLOCK_ORDER:
        block = blocks[name]
        letter = BLOCK_TAGS[name]
        tagged = []
        for index, row in enumerate(block["rows"]):
            tag = f"{letter}{index + 1}"
            tagged.append({**row, "tag": tag})
            detail = row.get("detail") or {}
            shown.append({
                "tag": tag, "block": name, "ref": row.get("ref") or "",
                "statement": _trim(row.get("text"), 420), "period": row.get("period"),
                "company": detail.get("company") or "", "at": str(detail.get("at") or row.get("period") or ""),
                "kind": detail.get("kind"), "kind_label": detail.get("kind_label"),
                "house": detail.get("house"), "title": detail.get("title"),
                "first_hand": bool(detail.get("first_hand")), "street": bool(detail.get("street")),
                "evidence": name in EVIDENCE_BLOCKS and not detail.get("our_own"),
                "source": detail.get("source"),
                # What a figure citing this row is checked against: the whole
                # passage for a passage, the row's own words otherwise.
                "source_text": detail.get("source_text") or row.get("text") or "",
            })
        block["rows"] = tagged
        ordered.append(block)

    sufficiency = question_sufficiency(kind["kind"], question=question, dates=dates, windows=windows,
                                       shown=shown, companies=named, retrieval_status=retrieval["status"])
    sufficiency["tried"] = tried_summary(retrieval, windows, sufficiency)
    context = {
        "schema_version": SCHEMA_VERSION,
        "question": question,
        "question_kind": kind["kind"],
        "question_kind_matched": kind["matched"],
        "wants_market_vs_us": kind["kind"] in VIEW_KINDS,
        "subjects": subjects,
        "companies": [show(ref) for ref in (named or universe)],
        "dates": dates,
        "windows": windows,
        "goal": {"title": mission.get("title"), "objective": mission.get("objective"),
                 "research_questions": list(mission.get("research_questions") or [])},
        "blocks": ordered,
        "shown": shown,
        "retrieval": retrieval,
        "material_packs": packs,
        "sufficiency": sufficiency,
        "carried_passages": len(carried),
        "passages_dropped_for_budget": dropped,
        "claims_considered": len(blocks["claims"]["rows"]),
        "claims_total": len(claims),
        "duplicates_dropped": duplicates_dropped,
        "budget_chars": budget_chars,
        "spent_chars": cost(),
        "missing": [{"block": block["block"], "label": block["label"], "reason": block["reason"]}
                    for block in ordered if not block["available"]],
    }
    context["context_hash"] = context_identity(context)
    return context


# ---------------------------------------------------------------------------
# sufficiency: is there first-hand material inside the asked-about window?
# ---------------------------------------------------------------------------

#: What an ask MaterialRequest asks the acquisition service for, per type.
REQUEST_DOC_KINDS: Mapping[str, tuple[str, ...]] = {
    "why_move": ("sales_note", "broker_report", "web_page", "sec_8k"),
    "what_happened": ("sec_8k", "sec_exhibit", "web_page", "sales_note"),
    "forecast": ("transcript", "sec_exhibit", "broker_report"),
    "valuation": ("broker_report",),
    "risk": ("broker_report", "sec_10k", "transcript"),
}


def question_sufficiency(kind: str, *, question: str, dates: Mapping[str, Any], windows: Mapping[str, Any],
                         shown: Sequence[Mapping[str, Any]], companies: Sequence[str],
                         retrieval_status: str) -> dict[str, Any]:
    """``sufficient`` when a first-hand passage lies inside the asked-about window.

    For a dated question (why it moved, what happened) the window is the day
    or range asked about; for the others it is the context window.  A broker
    or sales note inside the window keeps the status ``partial`` (the question
    can be answered, the company's own words are still missing).  ``low``
    names the MaterialRequest to open (``request``).
    """

    window = windows["primary"] if kind in DATED_KINDS else windows["context"]
    inside = [row for row in shown if row["block"] == "passages"
              and window["date_from"] <= str(row.get("period") or "")[:10] <= window["date_to"]]
    first_hand = [row for row in inside if row.get("first_hand")]
    street = [row for row in inside if row.get("street")]
    if first_hand:
        status = "sufficient"
    elif street:
        status = "partial"
    else:
        status = "low"
    reason = {
        "sufficient": f"窗口内有 {len(first_hand)} 段一手材料",
        "partial": f"窗口内没有一手材料，只有 {len(street)} 段券商/销售快评",
        "low": ("这个研究环境还没有全文证据索引" if retrieval_status == "no_index"
                else "窗口内没有一手材料，也没有券商/销售快评"),
    }[status]
    request = None
    # No index at all is an environment gap, not missing material: nothing
    # is asked of the acquisition service until the index exists.
    if status != "sufficient" and companies and retrieval_status != "no_index":
        doc_kinds = REQUEST_DOC_KINDS.get(kind, ("sec_8k", "transcript", "broker_report", "sales_note"))
        when = dates.get("anchor") or f"{window['date_from']} 至 {window['date_to']}"
        what = {
            "why_move": f"解释 {when} 股价变动的材料（当日公告、券商与销售快评、新闻）",
            "what_happened": f"{when} 期间的公司公告与新闻",
        }.get(kind, "回答这个问题所需的一手材料")
        request = {
            "subject": companies[0],
            "need": {"kind": "answer", "question": question[:2000], "doc_kinds": list(doc_kinds)},
            "period": f"{window['date_from']}..{window['date_to']}",
            "acceptance": {"date_from": window["date_from"], "date_to": window["date_to"],
                           "doc_kinds": list(doc_kinds), "min_passages": 1},
            "what": what,
        }
    return {"status": status, "reason": reason, "window": dict(window),
            "first_hand_in_window": len(first_hand), "street_in_window": len(street),
            "request": request}


def tried_summary(retrieval: Mapping[str, Any], windows: Mapping[str, Any], sufficiency: Mapping[str, Any]) -> str:
    """「已尝试：…」 in one sentence: where retrieval looked and what it found."""

    if retrieval.get("status") == "no_index":
        return "这个研究环境还没有全文证据索引，原文段落检索没有运行"
    runs = sum(len(item.get("queries") or ()) for item in retrieval.get("per_subject") or ())
    window = sufficiency.get("window") or windows.get("primary") or {}
    return (f"在 {window.get('date_from')} 至 {window.get('date_to')} 检索了公司公告、新闻稿、电话会、券商研报与销售快评"
            f"（{runs} 次检索，背景窗口自 {windows['context']['date_from']} 起）；窗口内一手材料 "
            f"{sufficiency.get('first_hand_in_window', 0)} 段、券商与销售快评 {sufficiency.get('street_in_window', 0)} 段")


def context_identity(context: Mapping[str, Any]) -> str:
    """What this context *is*: the question, what was shown, what was missing."""

    return content_hash({
        "question": context.get("question"),
        "shown": [{k: row.get(k) for k in ("tag", "block", "ref", "statement", "period")}
                  for row in context.get("shown") or ()],
        "missing": [dict(row) for row in context.get("missing") or ()],
        "goal": context.get("goal"),
    })


def passage_refs(context: Mapping[str, Any]) -> list[str]:
    """Every passage ref the context showed (what "new material" is measured against)."""

    return [str(row.get("ref")) for row in context.get("shown") or () if row.get("block") == "passages"]


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def _render_header(block: str, row: Mapping[str, Any]) -> str:
    detail = row.get("detail") or {}
    if block == "passages":
        parts = [str(detail.get("kind_label") or detail.get("kind") or "资料")]
        if detail.get("house"):
            parts.append(str(detail["house"]))
        parts.append(str(detail.get("date") or "日期未知"))
        if detail.get("title"):
            parts.append(f"《{_trim(detail['title'], 120)}》")
        if detail.get("company"):
            parts.append("涉及：" + str(detail["company"]))
        if detail.get("first_hand"):
            parts.append("一手材料")
        if detail.get("in_window"):
            parts.append("在问题所指的时间窗口内")
        if detail.get("carried"):
            parts.append("上一回答引用过")
        return " · ".join(parts)
    if block == "numbers":
        return f"（{detail.get('company')} · {detail.get('source')}）"
    if block in {"events", "house_view", "claims", "move"}:
        return f"（{detail.get('company')}）" if detail.get("company") else ""
    return ""


def render_block(block: Mapping[str, Any]) -> str:
    """One block as the prompt shows it, tags and all."""

    letter = block["tag_letter"]
    if not block["available"]:
        return f"## {block['label']}：没有。原因：{block['reason']}\n"
    lines = [f"## {block['label']}（{len(block['rows'])} 条，标签 {letter}n）"]
    if block.get("note"):
        lines.append(block["note"])
    for index, row in enumerate(block["rows"]):
        tag = row.get("tag") or f"{letter}{index + 1}"
        head = _render_header(block["block"], row)
        period = row.get("period")
        if block["block"] == "passages":
            lines.append(f"[{tag}] {head}")
            lines.append(str(row.get("text") or ""))
        else:
            lines.append(f"[{tag}]" + (f" [{period}]" if period and block["block"] != "move" else "")
                         + (f" {head}" if head else "") + f" {row.get('text')}")
    return "\n".join(lines) + "\n"


def render_context(context: Mapping[str, Any]) -> str:
    goal = context["goal"]
    dates = context.get("dates") or {}
    sufficiency = context.get("sufficiency") or {}
    lines = [
        f"研究目标：{goal.get('title')} —— {goal.get('objective')}",
        f"今天的问题被判定为「{context['question_kind']}」类"
        + (f"（触发词：{context['question_kind_matched']}）" if context.get("question_kind_matched") else ""),
        "涉及公司：" + ("、".join(context["companies"]) or "（未点名，按整个覆盖名单）"),
        f"问题所指的时间：{dates.get('label') or '未指定'}"
        + (f"（由「{dates.get('phrase')}」解析）" if dates.get("phrase") else ""),
        f"材料充分性：{sufficiency.get('status')} —— {sufficiency.get('reason')}",
        "",
    ]
    for block in context["blocks"]:
        lines.append(render_block(block))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# follow-up questions: the previous answer, its citations and its passages
# ---------------------------------------------------------------------------

MAX_FOLLOWUP_QUESTION_CHARS = 300
MAX_FOLLOWUP_ANSWER_CHARS = 1500
MAX_FOLLOWUP_CITATIONS = 8
MAX_FOLLOWUP_CITATION_CHARS = 240
MAX_FOLLOWUP_PASSAGES = 6


def previous_turn(job: Mapping[str, Any]) -> dict[str, Any] | None:
    """What a follow-up carries from the answer it follows, bounded.

    The reviewed display text is preferred over the model's own wording; the
    citations keep their statement, company and period; and the passage refs
    the answer cited travel too, so the follow-up's context re-resolves those
    passages (hash-checked) and can cite them again.
    """

    result = job.get("result") if isinstance(job, Mapping) else None
    request = job.get("request") if isinstance(job, Mapping) else None
    if not isinstance(result, Mapping) or not isinstance(request, Mapping):
        return None
    answer = result.get("display_answer") or result.get("answer")
    if not isinstance(answer, str) or not answer.strip():
        return None
    citations = []
    refs: list[str] = []
    for row in result.get("citations") or ():
        if not isinstance(row, Mapping) or not isinstance(row.get("statement"), str):
            continue
        if row.get("block") == "passages" and isinstance(row.get("ref"), str) and len(refs) < MAX_FOLLOWUP_PASSAGES:
            refs.append(row["ref"])
        if len(citations) < MAX_FOLLOWUP_CITATIONS:
            citations.append({
                "statement": _trim(row["statement"], MAX_FOLLOWUP_CITATION_CHARS),
                "company": str(row.get("company") or "")[:80],
                "period": str(row.get("period_label") or row.get("period") or "")[:60],
            })
    context = result.get("context") if isinstance(result.get("context"), Mapping) else {}
    return {
        "job_id": str(job.get("job_id") or ""),
        "question": _trim(str(request.get("question") or ""), MAX_FOLLOWUP_QUESTION_CHARS),
        "answer": _trim(answer, MAX_FOLLOWUP_ANSWER_CHARS),
        "citations": citations,
        "passage_refs": refs,
        "question_kind": context.get("question_kind"),
    }


__all__ = [
    "AskContextError", "BLOCK_LABELS", "BLOCK_ORDER", "BLOCK_PRIORITY", "BLOCK_TAGS", "DATED_KINDS",
    "DEFAULT_BUDGET_CHARS", "EVIDENCE_BLOCKS", "FIRST_HAND_KINDS", "LEGACY_BLOCK_LABELS", "LEGACY_BLOCK_TAGS", "MAX_CLAIM_ROWS", "PROFILES", "QUESTION_KINDS",
    "ROW_CAPS", "SCHEMA_VERSION", "STREET_KINDS", "UNAVAILABLE_REASONS", "VIEW_KINDS", "build_context",
    "context_identity", "event_row", "move_block", "passage_refs", "previous_turn", "query_terms",
    "question_kind", "question_sufficiency", "render_block", "render_context", "resolve_dates", "tried_summary",
    "resolve_subjects", "retrieval_windows", "retrieve",
]
