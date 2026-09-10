"""P15a: everything this system knows that bears on one question, bounded.

Ask v1 showed the model one thing: up to four hundred formal Claims, oldest
first, filtered by whichever ticker appeared in the question.  That was the
whole context.  Since then the Core has grown a company file, a debate map, a
forecast model with drivers and realised actuals, a valuation snapshot, a daily
price series, a catalyst calendar, an event stream with judgements and
reflections, and the PM's own verdicts on what it has already been shown -- and
the question panel could see none of it.  An analyst asked "怎么看 ACN 的估值"
answered from quotations because quotations were all it had.

This module is the assembler.  It is deterministic (no model call, no clock
beyond the one handed in), read-only (a plain ``mode=ro`` connection; every
authority's own reader class wants a write handle to run its schema script, so
the selection is done here in SQL and only the authorities' *pure* helpers and
frozen vocabularies are imported), and bounded (a documented priority order and
a byte budget, so a Core with ten years of history produces the same shape of
prompt as a Core with a week of it).

Three rules the rest of the file is an implementation of.

**Absence is an answer.**  Every block is present in the output whether or not
it has anything in it, carrying ``available`` and, when false, a ``reason`` from
a closed list.  "This Core has no valuation authority", "this company has no
snapshot" and "the question named no company" are three different facts and a
reader who cannot tell them apart will draw the wrong conclusion from the same
blank.  This is also what lets the answer say *needs refresh* about the right
thing: an unknown is only actionable if you know which shelf was empty.

**Every row carries a tag and a ref.**  The prompt shows ``C7``/``D2``/``F4``;
the row behind the tag carries the version ref it came from.  A citation the
answer makes is checked against the tags that were shown, so an answer cannot
cite something it was never given, and the ref is what makes the citation
checkable afterwards.  Tags are per block by design: a reader of the finished
answer can see at a glance whether a sentence rests on a filed Claim or on our
own model.

**The budget spends on what was asked about.**  The priority order below is
fixed, and the question's kind promotes at most two blocks to the front of it.
Claims keep a reserved floor whatever else is competing, because ADR-0006 says
an answer comes from the Claims and a context that spent its whole budget on a
dossier would quietly stop being an answer from the Ledger.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, Callable

from .store import content_hash

SCHEMA_VERSION = "0.1"

# The same ceiling ask v1 used, kept so that the two are comparable and so that
# the model configuration's input bound is not the thing that changed.
DEFAULT_BUDGET_CHARS = 90_000
# Claims never fall below this share of the budget, however many other blocks
# are competing.  ADR-0006: the answer comes from the Claims.
CLAIMS_FLOOR_SHARE = 0.45

# What a question is about, decided by the words in it.  Closed: the kind
# selects which blocks are promoted and whether a market-vs-us block is
# required, so an unrecognised kind would silently change the contract.
QUESTION_KINDS: tuple[str, ...] = (
    "view",           # 你怎么看 / what do you think
    "valuation",      # 贵不贵 / what multiple
    "outlook",        # 下季度会怎样 / what do you forecast
    "event_impact",   # X 对 Y 有什么影响
    "debate",         # 市场在吵什么 / bull and bear case
    "catalyst",       # 下次什么时候 / when is the print
    "fact",           # 多少 / what was the number
    "other",
)

# The kinds for which an answer must state where we differ from the market.
# The owner's instruction of 2026-09-09: agreeing with the market is worth
# nothing, so a view question is answered with a variant view and a pathway or
# it is not answered.  A debate question is included because "what is the
# market arguing about" is the same question asked from the other side.
VIEW_KINDS: frozenset[str] = frozenset({"view", "debate"})

# Ordered: the first pattern that matches wins, so a question that is both a
# view question and a valuation question is a view question.  Every pattern is
# written for both languages because the owner asks in both.
_KIND_PATTERNS: tuple[tuple[str, str], ...] = (
    ("view", r"怎么看|看法|观点|看多|看空|该不该|值不值得|要不要买|"
             r"what do you think|your view|our view|bull(ish)?|bear(ish)?|"
             r"should we (buy|own|sell)|do you like"),
    ("debate", r"争议|分歧|在吵|多空|市场担心|bear case|bull case|debate|"
               r"what is the market (arguing|worried)|controvers"),
    ("valuation", r"估值|贵|便宜|多少倍|市盈|倍数|分位|valuation|expensive|cheap|"
                  r"multiple|p/?e\b|ev/ebitda|fcf yield|percentile"),
    ("outlook", r"预测|预计|预期|下季度|下个季度|明年|未来|会不会|将会|指引|"
                r"forecast|outlook|guidance|estimate|next (quarter|year)|"
                r"do you expect|going to"),
    ("event_impact", r"影响|意味着|冲击|利好|利空|impact|implication|mean for|"
                     r"affect|read.?through"),
    ("catalyst", r"什么时候|何时|下一次|下次|日程|财报日|when (is|does|will)|next "
                 r"(earnings|print|catalyst|report)|calendar"),
    ("fact", r"多少|几个|是多少|数字|怎么样|如何|利用率|利润率|人员流失|"
             r"how much|how many|what was|what were|"
             r"revenue|margin|growth|bookings|headcount"),
)

# One label per block, in the owner's language.  A block with no label here
# would render under its key, which is ugly; the test pins that none is.
BLOCK_LABELS: Mapping[str, str] = {
    "theses": "我们已经立下的论点",
    "dossier": "公司档案（按主题分节）",
    "debates": "还在争的问题",
    "claims": "账本里的结论",
    "forecast": "我们的模型：驱动因素、假设与已实现",
    "valuation": "估值快照",
    "market_price": "最新股价",
    "consensus": "市场一致预期",
    "catalyst": "下一个日程",
    "events": "最近发生的事",
    "judgements": "我们对这些事的判断",
    "reflections": "事后反思",
    "journal": "你上次对答案的反馈",
    # Added by ask_refresh's second pass; never built by build_context, which
    # is why it is not in BLOCK_PRIORITY: it cannot be dropped for budget
    # because it did not exist when the budget was spent.
    "refreshed": "刚补搜到的文件（只有标题，正文还没读）",
}

# The tag letter each block's rows carry.  Distinct per block so a reader of
# the finished answer can see whether a sentence rests on a filed Claim (C) or
# on our own model (F) without opening anything.
BLOCK_TAGS: Mapping[str, str] = {
    "theses": "T", "dossier": "D", "debates": "B", "claims": "C",
    "forecast": "F", "valuation": "V", "market_price": "P", "consensus": "S",
    "catalyst": "K", "events": "E", "judgements": "G", "reflections": "R",
    "journal": "N", "refreshed": "H",
}

# The fixed priority order.  Read it as "if only one block fits, which".
# Claims are not in the list because they are never dropped; they are charged
# first, against their floor, and given whatever is left at the end.
BLOCK_PRIORITY: tuple[str, ...] = (
    "theses",
    "dossier",
    "debates",
    "forecast",
    "valuation",
    "market_price",
    "consensus",
    "catalyst",
    "events",
    "judgements",
    "reflections",
    "journal",
)

# Which blocks a question's kind moves to the front.  At most two, because
# promoting more is the same as having no priority order.
KIND_PROMOTIONS: Mapping[str, tuple[str, ...]] = {
    "view": ("dossier", "debates"),
    "valuation": ("valuation", "market_price"),
    "outlook": ("forecast", "consensus"),
    "event_impact": ("events", "judgements"),
    "debate": ("debates", "consensus"),
    "catalyst": ("catalyst", "events"),
    "fact": ("claims", "forecast"),
    "other": (),
}

# Why a block has nothing in it.  Closed, because the answer's ``unknowns``
# name one of these and a refresh decision is made from it.
UNAVAILABLE_REASONS: tuple[str, ...] = (
    # the authority's table is not on this Core at all
    "no_authority_on_this_core",
    # the authority is here and holds nothing for the resolved subjects
    "no_record_for_this_company",
    # the block is per company and the question named none
    "no_company_resolved",
    # the reader for it is not merged yet (consensus, P11b)
    "reader_not_available",
    # it had rows and the byte budget did not reach it
    "dropped_for_budget",
)

# How many rows each block may contribute before the budget is even consulted.
# A cap rather than a byte share: a hundred events would crowd out everything
# else long before they became useful, and the newest are the ones that matter.
ROW_CAPS: Mapping[str, int] = {
    "theses": 8, "dossier": 12, "debates": 8, "forecast": 24, "valuation": 8,
    "market_price": 5, "consensus": 8, "catalyst": 5, "events": 12,
    "judgements": 8, "reflections": 4, "journal": 6,
}
MAX_CLAIM_ROWS = 400
# One row's text is trimmed here rather than by the budget, so that dropping a
# block is always a decision about the block and never about one long sentence.
MAX_ROW_CHARS = 700


class AskContextError(RuntimeError):
    """The assembler was asked for something it cannot build."""


# ---------------------------------------------------------------------------
# question resolution
# ---------------------------------------------------------------------------

def question_kind(question: str) -> dict[str, Any]:
    """What this question is about, and which words said so.

    The matched words travel with the answer: a kind nobody can check is a
    classifier the owner has to trust, and the one thing this panel cannot ask
    for is trust.
    """

    lowered = (question or "").lower()
    for kind, pattern in _KIND_PATTERNS:
        match = re.search(pattern, lowered)
        if match is not None:
            return {"kind": kind, "matched": match.group(0)}
    return {"kind": "other", "matched": None}


def resolve_subjects(
    question: str,
    members: Mapping[str, Mapping[str, Any]],
    *,
    company_names: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Which companies (and whether the industry) the question is about.

    A question that names nobody is about the whole coverage list, and says so
    as ``universe`` rather than pretending it named them: the difference
    decides whether a per-company block is ``no_company_resolved`` or simply
    long.
    """

    names = dict(company_names or {})
    lowered = (question or "").lower()
    words = set(re.findall(r"[a-z]+", lowered))
    named: list[str] = []
    for ref, member in members.items():
        ticker = str(member.get("ticker") or "")
        name = names.get(ticker, "")
        if ticker.lower() in words or (name and name.lower() in lowered):
            named.append(ref)
    industry = bool(re.search(r"行业|同业|板块|industry|sector|peers|竞争对手", lowered))
    return {
        "companies": sorted(named),
        "scope": "named" if named else "universe",
        "industry": industry or not named,
        "all_companies": sorted(members),
    }


def _aspects_named(question: str) -> list[str]:
    """The dossier sections the question's words point at, in vocabulary order.

    Used only to choose which sections of a long file to excerpt.  An empty
    list means "no section was named", and the assembler then takes the file's
    order rather than inventing a relevance ranking it cannot defend.
    """

    from .claim_aspect_vocabulary import ASPECTS

    lowered = (question or "").lower()
    hints: Mapping[str, str] = {
        "business_model": r"商业模式|怎么赚钱|business model|how.*make money|contract",
        "segments_and_mix": r"业务构成|分部|结构|segment|mix|geograph",
        "demand_drivers": r"需求|订单|bookings|demand|pipeline|backlog",
        "supply_and_cost": r"成本|人力|薪酬|利用率|cost|wage|utilis|utiliz|supply|headcount",
        "competitive_position": r"竞争|份额|壁垒|competit|share|moat|gcc",
        "management_and_capital_allocation": r"管理层|回购|分红|并购|资本配置|"
                                             r"management|buyback|dividend|capital allocation|m&a",
        "guidance_style": r"指引|guidance|guide|beat|raise",
        "kpi_dictionary": r"口径|定义|指标|kpi|definition|metric",
        "catalyst_calendar": r"日程|催化|财报日|catalyst|calendar|earnings date",
        "history_of_price_drivers": r"股价|涨|跌|走势|price|stock|move|drawdown",
    }
    found = [aspect for aspect in ASPECTS if aspect in hints
             and re.search(hints[aspect], lowered)]
    return found


# ---------------------------------------------------------------------------
# small SQL helpers, all read-only
# ---------------------------------------------------------------------------

def _table_exists(connection: Any, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,),
    ).fetchone() is not None


def _latest_by(connection: Any, table: str, key: str, order: str) -> list[Any]:
    """The newest row of every chain, in one statement.

    The same join the cockpit's company cards use: parsing every superseded
    version to throw it away costs the whole history twice.
    """

    return connection.execute(
        f"SELECT t.* FROM {table} t JOIN (SELECT {key} AS k, MAX({order}) AS n "
        f"FROM {table} GROUP BY {key}) newest ON newest.k=t.{key} "
        f"AND newest.n=t.{order}"
    ).fetchall()


def _trim(text: Any, limit: int = MAX_ROW_CHARS) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _block(
    name: str, rows: Sequence[Mapping[str, Any]], *,
    available: bool | None = None, reason: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    if reason is not None and reason not in UNAVAILABLE_REASONS:
        raise AskContextError(f"{reason!r} is not a reason a block can be absent")
    kept = [dict(row) for row in rows][: ROW_CAPS.get(name, 12)]
    return {
        "block": name,
        "label": BLOCK_LABELS[name],
        "tag_letter": BLOCK_TAGS[name],
        "available": bool(kept) if available is None else bool(available),
        "reason": reason if not kept else None,
        "note": note,
        "rows": kept,
    }


# ---------------------------------------------------------------------------
# the blocks
# ---------------------------------------------------------------------------

def _theses_block(theses: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    rows = []
    for thesis in theses:
        statement = (thesis.get("summary") or thesis.get("statement")
                     or thesis.get("change_reason") or "")
        if not statement:
            continue
        rows.append({
            "ref": thesis.get("id") or thesis.get("thesis_ref"),
            "text": _trim(statement),
            "period": None,
            "detail": {"confidence": thesis.get("confidence")},
        })
    return _block("theses", rows, reason="no_record_for_this_company")


def _dossier_block(
    core: Any, companies: Sequence[str], aspects: Sequence[str], label: Callable[[str], str],
) -> dict[str, Any]:
    if not _table_exists(core, "company_dossier_versions"):
        return _block("dossier", (), available=False, reason="no_authority_on_this_core")
    if not companies:
        return _block("dossier", (), available=False, reason="no_company_resolved")
    from .company_dossier import section_body

    wanted = set(aspects)
    rows: list[dict[str, Any]] = []
    for company in companies:
        row = core.execute(
            "SELECT record_json FROM company_dossier_versions WHERE company_ref=? "
            "ORDER BY version_number DESC LIMIT 1", (company,),
        ).fetchone()
        if row is None:
            continue
        record = json.loads(row["record_json"])
        sections = [s for s in record.get("sections") or []
                    if s.get("status") == "drafted"]
        # A named section wins; nothing named means the file's own order, which
        # is the aspect vocabulary's order, which is the order an analyst reads
        # a company in.  No relevance score: there is nothing to compute one
        # from that would not be a guess wearing a number.
        chosen = [s for s in sections if s["aspect"] in wanted] or sections
        for section in chosen:
            rows.append({
                "ref": record.get("id"),
                "text": _trim(section_body(section)),
                "period": None,
                "detail": {
                    "company": label(company), "aspect": section["aspect"],
                    "version": record.get("version"),
                    "sources": [s["ref"] for s in section.get("sources") or []][:8],
                    "gaps": list(section.get("gaps") or [])[:3],
                },
            })
        variant = record.get("variant_view") or {}
        if variant.get("status") == "drafted":
            rows.append({
                "ref": record.get("id"),
                "text": _trim(section_body(variant)),
                "period": None,
                "detail": {
                    "company": label(company), "aspect": "variant_view",
                    "version": record.get("version"),
                    "market_view_available": variant.get("market_view_available"),
                    "market_view_reason": variant.get("market_view_reason"),
                    "sources": [s["ref"] for s in variant.get("sources") or []][:8],
                },
            })
    return _block("dossier", rows, reason="no_record_for_this_company")


def _debates_block(
    core: Any, subjects: Sequence[str], label: Callable[[str], str],
) -> dict[str, Any]:
    if not _table_exists(core, "debate_map_versions"):
        return _block("debates", (), available=False, reason="no_authority_on_this_core")
    from .debate_map import LIVE_STATUSES, map_ref_for

    rows: list[dict[str, Any]] = []
    for subject in subjects:
        row = core.execute(
            "SELECT record_json FROM debate_map_versions WHERE map_ref=? "
            "ORDER BY version_number DESC LIMIT 1", (map_ref_for(subject),),
        ).fetchone()
        if row is None:
            continue
        record = json.loads(row["record_json"])
        for debate in record.get("debates") or []:
            if debate.get("status") not in LIVE_STATUSES:
                continue
            market = debate.get("market_position") or {}
            ours = debate.get("our_position") or {}
            rows.append({
                "ref": record.get("id"),
                "text": _trim(debate.get("question")),
                "period": None,
                "detail": {
                    "subject": label(subject),
                    "debate_ref": debate.get("debate_ref"),
                    "status": debate.get("status"),
                    "bull": _trim((debate.get("bull_position") or {}).get("statement"), 240),
                    "bear": _trim((debate.get("bear_position") or {}).get("statement"), 240),
                    "market_lean": market.get("lean") if market.get("available") else None,
                    "market_view": (_trim(market.get("statement"), 240)
                                    if market.get("available") else None),
                    "our_side": ours.get("side") if ours.get("state") == "held" else None,
                    "our_view": (_trim(ours.get("statement"), 240)
                                 if ours.get("state") == "held" else None),
                    "last_shift_reason": (debate.get("last_shift_reason") or {}).get("reason"),
                },
            })
    return _block("debates", rows, reason="no_record_for_this_company")


def _claims_rows(
    claims: Sequence[Mapping[str, Any]], label: Callable[[str], str],
) -> list[dict[str, Any]]:
    """Canonical Claims as context rows, grouped by aspect and newest last.

    The order inside an aspect is the order ask v1 used -- oldest first -- for
    the reason it gave: which claims survive the budget is decided by recency,
    and re-sorting by importance here would change which ones the model ever
    sees.  The grouping is new: 2,170 atomic quotations in arrival order is the
    pile P12b exists to end.
    """

    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for claim in claims:
        grouped.setdefault(str(claim.get("index_aspect") or "untagged"), []).append(claim)
    rows: list[dict[str, Any]] = []
    for aspect in sorted(grouped):
        for claim in grouped[aspect]:
            rows.append({
                "ref": claim.get("ref"),
                "text": _trim(claim.get("statement")),
                "period": claim.get("period"),
                "detail": {
                    "company": label(str(claim.get("subject_ref") or "")),
                    "aspect": aspect,
                    "importance": claim.get("importance"),
                    "at": str(claim.get("created_at") or "")[:10],
                    "value": claim.get("value"),
                    "unit": claim.get("unit"),
                },
            })
    return rows


def _forecast_block(
    core: Any, companies: Sequence[str], label: Callable[[str], str],
) -> dict[str, Any]:
    if not _table_exists(core, "forecast_model_versions"):
        return _block("forecast", (), available=False, reason="no_authority_on_this_core")
    if not companies:
        return _block("forecast", (), available=False, reason="no_company_resolved")
    from .model_forecast_driver import model_readiness

    rows: list[dict[str, Any]] = []
    note = None
    for company in companies:
        row = core.execute(
            "SELECT record_json FROM forecast_model_versions WHERE company_ref=? "
            "ORDER BY version_number DESC LIMIT 1", (company,),
        ).fetchone()
        if row is None:
            continue
        record = json.loads(row["record_json"])
        readiness = model_readiness(record)
        note = (f"{label(company)} 的模型：{readiness['forecast_quarters']} 个未来季度，"
                f"{readiness['drivers_with_assumptions']}/{readiness['drivers']} "
                f"条驱动因素有假设，{readiness['actual_cells']} 格已被实际数取代")
        assumptions: dict[str, Mapping[str, Any]] = {}
        for item in record.get("assumptions") or []:
            if item.get("superseded_by") is None:
                assumptions[str(item.get("driver_ref"))] = item
        for driver in record.get("drivers") or []:
            assumption = assumptions.get(str(driver.get("ref")))
            rows.append({
                "ref": record.get("id"),
                "text": _trim(f"{driver.get('label')}（{driver.get('role') or '未定角色'}）"),
                "period": None if assumption is None else (assumption.get("period") or {}).get("end"),
                "detail": {
                    "company": label(company), "driver_ref": driver.get("ref"),
                    "status": driver.get("status"),
                    "assumption": None if assumption is None else {
                        "measure": assumption.get("measure"),
                        "value": assumption.get("value"),
                        "unit": assumption.get("unit"),
                        "kind": assumption.get("kind"),
                        "because": _trim(assumption.get("because"), 240),
                        "refs": [r.get("ref") for r in (assumption.get("refs") or [])][:6],
                    },
                },
            })
        for line in record.get("results") or []:
            cells = list(line.get("cells") or [])
            latest_actual = next((c for c in reversed(cells) if c.get("kind") == "actual"), None)
            latest_estimate = next((c for c in reversed(cells) if c.get("kind") == "estimate"), None)
            rows.append({
                "ref": record.get("id"),
                "text": _trim(f"{line.get('label')}：{line.get('status')}"),
                "period": None if latest_estimate is None else (latest_estimate.get("period") or {}).get("end"),
                "detail": {
                    "company": label(company), "result_ref": line.get("ref"),
                    "role": line.get("role"), "unit": line.get("unit"),
                    "reason": line.get("reason"),
                    "latest_estimate": None if latest_estimate is None else {
                        "period_end": (latest_estimate.get("period") or {}).get("end"),
                        "value": latest_estimate.get("value"),
                        "status": latest_estimate.get("status"),
                        "superseded_by": latest_estimate.get("superseded_by"),
                    },
                    "latest_actual": None if latest_actual is None else {
                        "period_end": (latest_actual.get("period") or {}).get("end"),
                        "value": latest_actual.get("value"),
                    },
                },
            })
    return _block("forecast", rows, reason="no_record_for_this_company", note=note)


def _valuation_block(
    core: Any, companies: Sequence[str], label: Callable[[str], str],
) -> dict[str, Any]:
    if not _table_exists(core, "valuation_snapshot_versions"):
        return _block("valuation", (), available=False, reason="no_authority_on_this_core")
    if not companies:
        return _block("valuation", (), available=False, reason="no_company_resolved")
    rows: list[dict[str, Any]] = []
    for company in companies:
        row = core.execute(
            "SELECT record_json FROM valuation_snapshot_versions WHERE company_ref=? "
            "ORDER BY version_number DESC LIMIT 1", (company,),
        ).fetchone()
        if row is None:
            continue
        record = json.loads(row["record_json"])
        for metric in record.get("metrics") or []:
            percentile = metric.get("percentile") or {}
            if metric.get("status") == "available":
                text = (f"{metric.get('label') or metric.get('metric')} = "
                        f"{metric.get('value')}{metric.get('unit') or ''}")
                if percentile.get("value") is not None:
                    text += f"，历史分位 {percentile['value']}"
            else:
                text = (f"{metric.get('label') or metric.get('metric')}：算不出来"
                        f"（{metric.get('reason') or '未说明'}）")
            rows.append({
                "ref": record.get("id"),
                "text": _trim(text),
                "period": record.get("as_of"),
                "detail": {
                    "company": label(company), "metric": metric.get("metric"),
                    "status": metric.get("status"),
                    # The basis travels with the percentile, never in a
                    # footnote: a percentile computed over a stretch in which
                    # the filed fundamentals never moved is the price's own
                    # percentile, and an answer that does not say so is telling
                    # the owner the company is cheap against its own history.
                    "percentile_basis": percentile.get("basis"),
                    "percentile_reason": percentile.get("reason"),
                    "sample_size": percentile.get("sample_size"),
                    "as_of": record.get("as_of"), "currency": record.get("currency"),
                },
            })
    return _block("valuation", rows, reason="no_record_for_this_company")


def _price_block(
    core: Any, companies: Sequence[str], label: Callable[[str], str],
) -> dict[str, Any]:
    if not _table_exists(core, "market_price_series_versions"):
        return _block("market_price", (), available=False, reason="no_authority_on_this_core")
    if not companies:
        return _block("market_price", (), available=False, reason="no_company_resolved")
    from .market_price import bar_is_provisional

    wanted = set(companies)
    rows: list[dict[str, Any]] = []
    for row in _latest_by(core, "market_price_series_versions", "series_ref", "version_number"):
        record = json.loads(row["record_json"])
        if record.get("company_ref") not in wanted:
            continue
        bars = record.get("bars") or []
        if not bars:
            continue
        newest = bars[-1]
        provisional = bar_is_provisional(newest)
        rows.append({
            "ref": record.get("id"),
            "text": _trim(
                f"{label(str(record['company_ref']))} 最新收盘 {newest['close']} "
                f"{record.get('currency') or ''}（{newest['date']}）"
                + ("；这是盘中价，当天还没有收盘定价" if provisional else "")),
            "period": newest["date"],
            "detail": {
                "company": label(str(record["company_ref"])),
                "close": newest["close"], "adj_close": newest.get("adj_close"),
                "as_of": newest["date"], "provisional": provisional,
                "bars": len(bars), "since": record.get("first_bar_date"),
            },
        })
    return _block("market_price", rows, reason="no_record_for_this_company")


def _catalyst_block(
    core: Any, companies: Sequence[str], label: Callable[[str], str], today: str,
) -> dict[str, Any]:
    if not _table_exists(core, "catalyst_calendar_versions"):
        return _block("catalyst", (), available=False, reason="no_authority_on_this_core")
    if not companies:
        return _block("catalyst", (), available=False, reason="no_company_resolved")
    rows: list[dict[str, Any]] = []
    for company in companies:
        row = core.execute(
            "SELECT record_json FROM catalyst_calendar_versions WHERE company_ref=? "
            "ORDER BY version_number DESC LIMIT 1", (company,),
        ).fetchone()
        if row is None:
            continue
        record = json.loads(row["record_json"])
        forthcoming = [e for e in record.get("entries") or []
                       if str(e.get("expected_date") or "") >= today]
        if not forthcoming:
            continue
        entry = min(forthcoming, key=lambda item: (
            item["expected_date"], item.get("event_kind") or "",
            item.get("anchor_date") or ""))
        rows.append({
            "ref": record.get("id"),
            "text": _trim(f"{label(company)}：{entry.get('event_kind')} "
                          f"预计 {entry.get('expected_date')}"
                          + ("（已确认）" if entry.get("confirmed") else "（推算，未确认）")),
            "period": entry.get("expected_date"),
            "detail": {
                "company": label(company), "event_kind": entry.get("event_kind"),
                "expected_date": entry.get("expected_date"),
                "confirmed": entry.get("confirmed"),
                "basis": entry.get("basis"), "caveat": entry.get("caveat"),
            },
        })
    return _block("catalyst", rows, reason="no_record_for_this_company")


def _consensus_block(
    companies: Sequence[str], label: Callable[[str], str],
    reader: Callable[[str], Mapping[str, Any] | None] | None,
) -> dict[str, Any]:
    """P11b's consensus, when P11b is on this Core.

    The reader is injected because the consensus authority is on a branch that
    has not merged.  A missing reader is ``reader_not_available`` and not
    ``no_record``: "we have no consensus authority" is a gap in the system and
    "the street has not published on this name" is a fact about the name, and
    an answer that confuses them will tell the owner the street is silent when
    in truth nobody has looked.
    """

    if reader is None:
        return _block("consensus", (), available=False, reason="reader_not_available")
    if not companies:
        return _block("consensus", (), available=False, reason="no_company_resolved")
    rows: list[dict[str, Any]] = []
    for company in companies:
        record = reader(company)
        if not record:
            continue
        for item in record.get("estimates") or []:
            rows.append({
                "ref": record.get("id") or record.get("ref"),
                "text": _trim(f"{label(company)} {item.get('metric')} "
                              f"{item.get('period')}：一致预期 {item.get('value')} "
                              f"{item.get('unit') or ''}"),
                "period": item.get("period"),
                "detail": {
                    "company": label(company), "metric": item.get("metric"),
                    "value": item.get("value"), "unit": item.get("unit"),
                    "contributors": item.get("contributors"),
                    "as_of": record.get("as_of"), "source": record.get("source"),
                },
            })
    return _block("consensus", rows, reason="no_record_for_this_company")


def _events_block(
    core: Any, companies: Sequence[str], label: Callable[[str], str],
) -> dict[str, Any]:
    if not _table_exists(core, "research_events"):
        return _block("events", (), available=False, reason="no_authority_on_this_core")
    if not companies:
        return _block("events", (), available=False, reason="no_company_resolved")
    marks = ",".join("?" for _ in companies)
    rows = []
    for row in core.execute(
        f"SELECT event_id, company_ref, kind, occurred_at, evidence_tier, record_json "
        f"FROM research_events WHERE company_ref IN ({marks}) "
        f"ORDER BY occurred_at DESC, event_id DESC LIMIT ?",
        (*companies, ROW_CAPS["events"]),
    ).fetchall():
        record = json.loads(row["record_json"])
        payload = record.get("payload") or {}
        headline = (payload.get("title") or payload.get("statement")
                    or payload.get("event_kind") or row["kind"])
        rows.append({
            "ref": row["event_id"],
            "text": _trim(f"{label(row['company_ref'])} · {row['kind']}：{headline}"),
            "period": row["occurred_at"][:10],
            "detail": {
                "company": label(row["company_ref"]), "kind": row["kind"],
                "occurred_at": row["occurred_at"],
                "evidence_tier": row["evidence_tier"],
                "source_refs": list(record.get("source_refs") or [])[:6],
            },
        })
    return _block("events", rows, reason="no_record_for_this_company")


def _judgements_block(
    core: Any, companies: Sequence[str], label: Callable[[str], str],
) -> dict[str, Any]:
    if not _table_exists(core, "event_judgements"):
        return _block("judgements", (), available=False, reason="no_authority_on_this_core")
    if not companies:
        return _block("judgements", (), available=False, reason="no_company_resolved")
    marks = ",".join("?" for _ in companies)
    rows = []
    for row in core.execute(
        f"SELECT judgement_id, company_ref, decision, action, event_ref, created_at, "
        f"record_json FROM event_judgements WHERE company_ref IN ({marks}) "
        f"ORDER BY created_at DESC, judgement_id DESC LIMIT ?",
        (*companies, ROW_CAPS["judgements"]),
    ).fetchall():
        record = json.loads(row["record_json"])
        rows.append({
            "ref": row["judgement_id"],
            "text": _trim(f"{label(row['company_ref'])}：{row['decision']} / "
                          f"{row['action']} —— {record.get('because') or ''}"),
            "period": row["created_at"][:10],
            "detail": {
                "company": label(row["company_ref"]), "decision": row["decision"],
                "action": row["action"], "event_ref": row["event_ref"],
                "driver_refs": list(record.get("driver_refs") or [])[:6],
                "thesis_refs": list(record.get("thesis_refs") or [])[:6],
            },
        })
    return _block("judgements", rows, reason="no_record_for_this_company")


def _reflections_block(
    core: Any, companies: Sequence[str], label: Callable[[str], str],
) -> dict[str, Any]:
    if not _table_exists(core, "thesis_reflections"):
        return _block("reflections", (), available=False, reason="no_authority_on_this_core")
    if not companies:
        return _block("reflections", (), available=False, reason="no_company_resolved")
    marks = ",".join("?" for _ in companies)
    rows = []
    for row in core.execute(
        f"SELECT reflection_id, company_ref, trigger_kind, created_at, record_json "
        f"FROM thesis_reflections WHERE company_ref IN ({marks}) "
        f"ORDER BY created_at DESC, reflection_id DESC LIMIT ?",
        (*companies, ROW_CAPS["reflections"]),
    ).fetchall():
        record = json.loads(row["record_json"])
        rows.append({
            "ref": row["reflection_id"],
            "text": _trim(f"{label(row['company_ref'])}：本以为「"
                          f"{record.get('what_we_expected') or ''}」，实际「"
                          f"{record.get('what_happened') or ''}」；"
                          f"{record.get('why') or ''}"),
            "period": row["created_at"][:10],
            "detail": {
                "company": label(row["company_ref"]),
                "trigger_kind": row["trigger_kind"],
                "missed_debates": [d.get("question") for d in
                                   (record.get("missed_debates") or [])][:4],
                "followup_research": [d.get("question") for d in
                                      (record.get("followup_research") or [])][:4],
            },
        })
    return _block("reflections", rows, reason="no_record_for_this_company")


def _journal_block(core: Any, label: Callable[[str], str]) -> dict[str, Any]:
    """What the PM said about the last answers, so the next one can do better.

    Only the outstanding verdicts: "read" and "useful" are the PM closing a
    loop, and re-showing them would be the panel congratulating itself with the
    owner's own words.
    """

    if not _table_exists(core, "analyst_journal_entries"):
        return _block("journal", (), available=False, reason="no_authority_on_this_core")
    from .cockpit_plane import OUTSTANDING_VERDICTS, VERDICT_LABELS

    marks = ",".join("?" for _ in sorted(OUTSTANDING_VERDICTS))
    rows = []
    for row in core.execute(
        f"SELECT target_ref, target_kind, company_ref, verdict, note, created_at "
        f"FROM analyst_journal_entries WHERE verdict IN ({marks}) "
        f"ORDER BY created_at DESC, entry_number DESC LIMIT ?",
        (*sorted(OUTSTANDING_VERDICTS), ROW_CAPS["journal"]),
    ).fetchall():
        rows.append({
            "ref": row["target_ref"],
            "text": _trim(f"你对「{row['target_kind']}」说了"
                          f"「{VERDICT_LABELS.get(row['verdict'], row['verdict'])}」"
                          + (f"：{row['note']}" if row["note"] else "")),
            "period": row["created_at"][:10],
            "detail": {
                "company": label(row["company_ref"] or ""),
                "verdict": row["verdict"], "target_kind": row["target_kind"],
            },
        })
    return _block("journal", rows, reason="no_record_for_this_company")


# ---------------------------------------------------------------------------
# the assembler
# ---------------------------------------------------------------------------

def build_context(
    core: Any,
    *,
    question: str,
    mission: Mapping[str, Any],
    members: Mapping[str, Mapping[str, Any]],
    claims: Sequence[Mapping[str, Any]],
    theses: Sequence[Mapping[str, Any]] = (),
    company_names: Mapping[str, str] | None = None,
    label: Callable[[str], str] | None = None,
    today: str,
    budget_chars: int = DEFAULT_BUDGET_CHARS,
    consensus_reader: Callable[[str], Mapping[str, Any] | None] | None = None,
    duplicates_dropped: int = 0,
) -> dict[str, Any]:
    """Everything that bears on this question, tagged, bounded and explained.

    ``claims`` are the canonical rows the caller has already read through
    P12b's index; this module does not re-derive them, because the cockpit's
    ``_indexed_claims`` is the one place that decides what canonical means and
    two implementations of that would drift.
    """

    if budget_chars < 1000:
        raise AskContextError("the context budget must leave room for the claims")
    naming = dict(company_names or {})
    show = label or (lambda ref: naming.get(
        str((members.get(ref) or {}).get("ticker") or ""), ref) or ref)
    kind = question_kind(question)
    subjects = resolve_subjects(question, members, company_names=naming)
    aspects = _aspects_named(question)
    companies = subjects["companies"] or subjects["all_companies"]
    debate_subjects = list(companies)
    industry_ref = mission.get("industry_ref")
    if subjects["industry"] and isinstance(industry_ref, str) and industry_ref:
        debate_subjects.append(industry_ref)

    # Claims are filtered to the named companies exactly as ask v1 did; a
    # question that named nobody keeps them all.
    pool = list(claims)
    if subjects["companies"]:
        named = set(subjects["companies"])
        chosen = [c for c in pool if c.get("subject_ref") in named]
        # The v1 rule, kept: a name with almost nothing on it gets the rest of
        # the Ledger rather than four rows and a shrug.
        pool = chosen if len(chosen) >= 20 else chosen + [
            c for c in pool if c.get("subject_ref") not in named]
    pool = pool[-MAX_CLAIM_ROWS:]

    # The claims block is the one block whose row count is not capped by
    # ROW_CAPS: it is capped by MAX_CLAIM_ROWS above and then by the budget.
    claim_rows = _claims_rows(pool, show)
    blocks: dict[str, dict[str, Any]] = {
        "theses": _theses_block(theses),
        "dossier": _dossier_block(core, companies, aspects, show),
        "debates": _debates_block(core, debate_subjects, show),
        "claims": {
            "block": "claims", "label": BLOCK_LABELS["claims"],
            "tag_letter": BLOCK_TAGS["claims"], "available": bool(claim_rows),
            "reason": None if claim_rows else "no_record_for_this_company",
            "note": None, "rows": claim_rows,
        },
        "forecast": _forecast_block(core, companies, show),
        "valuation": _valuation_block(core, companies, show),
        "market_price": _price_block(core, companies, show),
        "consensus": _consensus_block(companies, show, consensus_reader),
        "catalyst": _catalyst_block(core, companies, show, today),
        "events": _events_block(core, companies, show),
        "judgements": _judgements_block(core, companies, show),
        "reflections": _reflections_block(core, companies, show),
        "journal": _journal_block(core, show),
    }

    order = [name for name in KIND_PROMOTIONS.get(kind["kind"], ()) if name != "claims"]
    order += [name for name in BLOCK_PRIORITY if name not in order]

    # Charge the budget.  Claims first, against their floor; then the other
    # blocks in priority order; then the claims again against the floor plus
    # whatever the other blocks left, so a question with nothing else to show
    # gets the whole budget's worth of Ledger.
    claims_floor = int(budget_chars * CLAIMS_FLOOR_SHARE)
    kept_claims, claims_cost = _claims_that_fit(claim_rows, claims_floor)
    spent = claims_cost
    for name in order:
        block = blocks[name]
        if not block["available"]:
            continue
        cost = _block_cost(block)
        if spent + cost > budget_chars:
            block["available"] = False
            block["reason"] = "dropped_for_budget"
            block["rows"] = []
            continue
        spent += cost
    others_cost = spent - claims_cost
    kept_claims, claims_cost = _claims_that_fit(claim_rows, budget_chars - others_cost)
    spent = others_cost + claims_cost
    blocks["claims"]["rows"] = claim_rows[len(claim_rows) - kept_claims:] if kept_claims else []
    blocks["claims"]["available"] = bool(blocks["claims"]["rows"])
    if not blocks["claims"]["rows"] and claim_rows:
        blocks["claims"]["reason"] = "dropped_for_budget"

    shown: list[dict[str, Any]] = []
    ordered_blocks: list[dict[str, Any]] = []
    for name in ("claims", *BLOCK_PRIORITY):
        block = blocks[name]
        letter = BLOCK_TAGS[name]
        tagged = []
        for index, row in enumerate(block["rows"]):
            tag = f"{letter}{index + 1}"
            tagged.append({**row, "tag": tag})
            detail = row.get("detail") or {}
            shown.append({
                "tag": tag, "block": name, "ref": row.get("ref") or "",
                "statement": row.get("text") or "", "period": row.get("period"),
                # ``company`` and ``at`` are what the cockpit page prints under
                # a citation. They travel on the shown row rather than being
                # looked up again by the page, so that a citation to a
                # valuation row and a citation to a Claim render the same way.
                "company": detail.get("company") or detail.get("subject") or "",
                "at": str(detail.get("at") or row.get("period") or ""),
            })
        block["rows"] = tagged
        ordered_blocks.append(block)

    context = {
        "schema_version": SCHEMA_VERSION,
        "question": question,
        "question_kind": kind["kind"],
        "question_kind_matched": kind["matched"],
        "wants_market_vs_us": kind["kind"] in VIEW_KINDS,
        "aspects_named": aspects,
        "subjects": subjects,
        "companies": [show(ref) for ref in companies],
        "goal": {
            "title": mission.get("title"), "objective": mission.get("objective"),
            "research_questions": list(mission.get("research_questions") or []),
        },
        "blocks": ordered_blocks,
        "shown": shown,
        "claims_considered": len(blocks["claims"]["rows"]),
        "claims_total": len(claims),
        "duplicates_dropped": duplicates_dropped,
        "budget_chars": budget_chars,
        "spent_chars": spent,
        "missing": [
            {"block": block["block"], "label": block["label"], "reason": block["reason"]}
            for block in ordered_blocks if not block["available"]
        ],
    }
    context["context_hash"] = context_identity(context)
    return context


def context_identity(context: Mapping[str, Any]) -> str:
    """What this context *is*: the question, what was shown, what was missing.

    A function rather than an expression inside the assembler because the
    refresh's second pass adds a block and has to recompute it.  A context that
    kept the first pass's hash after gaining a block would be claiming the two
    answers were built from the same material, which is precisely the thing the
    hash exists to deny.
    """

    return content_hash({
        "question": context.get("question"),
        "shown": [dict(row) for row in context.get("shown") or ()],
        "missing": [dict(row) for row in context.get("missing") or ()],
        "goal": context.get("goal"),
    })


def _block_cost(block: Mapping[str, Any]) -> int:
    return len(render_block(block))


def _claims_that_fit(rows: Sequence[Mapping[str, Any]], budget: int) -> tuple[int, int]:
    """How many of the newest claim rows fit in ``budget``, and what they cost.

    Counted from the end of a list held oldest-first, so the rows that survive
    a tight budget are the recent ones -- and they are then rendered
    oldest-first again, which is the order the Ledger reads in.  Recomputed
    from scratch rather than topped up, so the answer is a function of the
    budget alone and does not depend on how many times it was asked.
    """

    spent = kept = 0
    for row in reversed(list(rows)):
        cost = len(str(row.get("text") or "")) + 90
        if spent + cost > budget:
            break
        spent += cost
        kept += 1
    return kept, spent


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

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
        head = f"{tag}"
        period = row.get("period")
        if period:
            head += f" [{period}]"
        detail = _render_detail(block["block"], row.get("detail") or {})
        lines.append(f"{head} {row.get('text')}" + (f"  {detail}" if detail else ""))
    return "\n".join(lines) + "\n"


def _render_detail(name: str, detail: Mapping[str, Any]) -> str:
    """The fields of a row that change what it means, and no others.

    Deliberately short.  A prompt that prints every field of every row spends
    its budget on JSON punctuation, and the fields chosen here are the ones a
    reader would be wrong without: which company, how much to believe it, and
    whether a number is ours or theirs.
    """

    if name == "claims":
        parts = [detail.get("company"), detail.get("aspect"), detail.get("importance"),
                 detail.get("at")]
    elif name == "dossier":
        parts = [detail.get("company"), detail.get("aspect"),
                 f"v{detail.get('version')}" if detail.get("version") else None]
    elif name == "debates":
        parts = [detail.get("subject"), detail.get("status"),
                 f"市场：{detail['market_lean']}" if detail.get("market_lean") else "市场立场未知",
                 f"我们：{detail['our_side']}" if detail.get("our_side") else "我们尚无立场"]
    elif name == "forecast":
        assumption = detail.get("assumption") or {}
        parts = [detail.get("company"), detail.get("role") or detail.get("status"),
                 (f"假设 {assumption.get('measure')}={assumption.get('value')}"
                  f"（{assumption.get('kind')}）" if assumption else None)]
    elif name == "valuation":
        parts = [detail.get("company"), detail.get("as_of"),
                 detail.get("percentile_basis")]
    elif name in {"events", "judgements"}:
        parts = [detail.get("company"), detail.get("evidence_tier") or detail.get("action")]
    else:
        parts = [detail.get("company")]
    return "（" + "；".join(str(p) for p in parts if p) + "）" if any(parts) else ""


def render_context(context: Mapping[str, Any]) -> str:
    """The whole context as the prompt shows it."""

    goal = context["goal"]
    lines = [
        f"研究目标：{goal['title']} —— {goal['objective']}",
        "长期研究问题：" + " | ".join(goal["research_questions"]),
        f"这个问题被判定为「{context['question_kind']}」类"
        + (f"（触发词：{context['question_kind_matched']}）"
           if context["question_kind_matched"] else ""),
        "涉及公司：" + ("、".join(context["companies"]) or "（未点名，按整个覆盖名单）"),
        "",
    ]
    for block in context["blocks"]:
        lines.append(render_block(block))
    return "\n".join(lines)


__all__ = [
    "AskContextError",
    "BLOCK_LABELS",
    "BLOCK_PRIORITY",
    "BLOCK_TAGS",
    "CLAIMS_FLOOR_SHARE",
    "DEFAULT_BUDGET_CHARS",
    "KIND_PROMOTIONS",
    "QUESTION_KINDS",
    "ROW_CAPS",
    "SCHEMA_VERSION",
    "UNAVAILABLE_REASONS",
    "VIEW_KINDS",
    "build_context",
    "context_identity",
    "question_kind",
    "render_block",
    "render_context",
    "resolve_subjects",
]
