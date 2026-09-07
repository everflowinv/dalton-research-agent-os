"""P10c: draft the Playbook's Initial Screen from Claims the Ledger holds.

The Playbook froze the document's eight sections and the four questions its
gate asks.  This module turns those into a bounded drafting pass:

- the context is the company's own live Claims, tagged ``C1..Cn``, plus its
  quantitative Claims tagged ``N1..Nk`` with the exact figure text;
- the model writes one section at a time and may cite only those tags.  A
  figure may appear in the body only if the section cites the ``N`` tag that
  carries it, so the number discipline is structural, not a request;
- the valuation section is not drafted at all: no market-data connector is
  admitted, and the Playbook says an unsourced number is worse than a gap;
- the exit gate is assessed by structural checks over the mission's own source
  base and the document that was written, never by asking a model whether its
  own work is good.

Nothing here writes: it produces the record the deliverable authority checks
and the gate assessment the stage ledger records.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from .mission_deliverable import GAP_MARKER, value_tokens

SCHEMA_VERSION = "0.1"
KIND = "initial_screen"
TEMPLATE_KEY = "initial_screen"
MAX_CLAIMS_PER_SECTION = 120
MAX_CONTEXT_CHARS = 60_000
VALUATION_TITLE_HINT = "估值"

# What each templated section is for, in the Playbook's own terms.  The titles
# come from the playbook record at run time; these are matched by position.
SECTION_GUIDANCE: tuple[str, ...] = (
    "用 3 到 6 句话写出关键信息与执行摘要：这家公司是做什么的、当前最重要的判断是什么、"
    "最需要盯住的一两件事是什么。",
    "写公司概览：主营业务、分部构成、收入与利润的来源，以及最近报告期里管理层强调的经营重点。",
    "写行业概览（简版）：需求来自哪里、竞争格局如何、这家公司在其中的位置。",
    "写核心 thesis（简版）：识别驱动价值创造或毁灭的关键 driver，论证它足以驱动业绩或股价显著变化，"
    "并说明市场（street）可能在哪里没有反映它。如果证据不足以支撑一个 thesis，直说不足在哪里。",
    "写风险与 anti-thesis（简版）：完整的反向观点，不是风险清单；逐条说明它成立需要什么条件。",
    "写 relevance to universe：这件事对覆盖范围内其他公司的多空含义。",
    "",  # valuation: never drafted, see VALUATION_GAP
    "写数据跟踪：应该盯住哪些可观察的高频或定期数据来验证或证伪上面的判断。",
)
VALUATION_GAP = (
    "估值一节按 Playbook 的数字纪律留空：市场价格、股本、汇率、利率与 consensus 五类正式 authority "
    "尚未接入，写任何倍数或目标价都会是无来源的数字。"
)
GATE_QUESTION_CHECKS = ("source_base", "number_provenance", "key_driver", "street_and_risk")


def section_titles(playbook: Mapping[str, Any]) -> list[str]:
    titles = (playbook.get("deliverable_templates") or {}).get(TEMPLATE_KEY) or []
    if not isinstance(titles, list) or not titles:
        raise ValueError("playbook carries no initial_screen template")
    return [str(title) for title in titles]


def build_claim_context(
    claims: Sequence[Mapping[str, Any]], *, max_claims: int = MAX_CLAIMS_PER_SECTION
) -> dict[str, Any]:
    """Tag the company's live Claims as C1..Cn (qualitative) and N1..Nk (figures)."""

    qualitative, quantitative = [], []
    for claim in claims:
        (quantitative if claim.get("value") is not None else qualitative).append(claim)
    tagged_claims, tagged_numbers = [], []
    budget = MAX_CONTEXT_CHARS
    for claim in qualitative[-max_claims:]:
        line = str(claim.get("statement") or "")
        if budget - len(line) < 0:
            break
        budget -= len(line)
        tagged_claims.append({
            "tag": f"C{len(tagged_claims) + 1}", "ref": claim["ref"], "statement": line,
            "period": claim.get("period"), "aspect": claim.get("aspect"),
            "created_at": claim.get("created_at"),
        })
    for claim in quantitative[-40:]:
        tagged_numbers.append({
            "tag": f"N{len(tagged_numbers) + 1}", "ref": claim["ref"],
            "statement": str(claim.get("statement") or ""),
            "figures": value_tokens(str(claim.get("statement") or "")),
            "period": claim.get("period"),
        })
    return {"claims": tagged_claims, "numbers": tagged_numbers}


def build_section_prompt(
    *,
    title: str,
    guidance: str,
    company: Mapping[str, Any],
    mission: Mapping[str, Any],
    context: Mapping[str, Any],
    checklist: Sequence[Mapping[str, Any]] = (),
) -> str:
    lines = [
        "You are drafting one section of an equity research Initial Screen for a fund's own file.",
        "Write in Chinese, in full sentences, for a portfolio manager who knows the sector.",
        "",
        f"Section: {title}",
        f"What this section is for: {guidance}",
        "",
        "Hard rules:",
        "- Use ONLY the tagged material below.  Cite the C tags you rely on.",
        "- You may write a figure ONLY by citing the N tag that carries it, and the figure must appear",
        f"  in that N tag's text verbatim.  If you need a number you do not have, write {GAP_MARKER}.",
        "- Do not invent company names, products, dates or numbers.  Do not repeat the section title.",
        "- If the material cannot support this section, say so in one sentence and list what is missing.",
        "",
        "Return raw JSON only, no markdown fence:",
        '{"body": "<the section text>", "claims": ["C3","C7"], "numbers": ["N1"],',
        ' "gaps": ["<what is missing to write this section properly>"]}',
        "",
        f"Company: {company.get('ticker')} ({company.get('company_ref')})",
        f"Mission goal: {mission.get('title')} — {mission.get('objective')}",
        "Standing research questions: " + " | ".join(mission.get("research_questions") or []),
    ]
    if checklist:
        missing = [item["label"] for item in checklist if item.get("status") in {"partial", "missing", "not_planned", "source_unavailable"}]
        if missing:
            lines.append("Source base still missing: " + "、".join(missing))
    lines.append("")
    lines.append(f"Figures available ({len(context['numbers'])}):")
    for item in context["numbers"]:
        lines.append(f"{item['tag']} [{item['period']}] {item['statement']}")
    lines.append("")
    lines.append(f"Statements available ({len(context['claims'])}):")
    for item in context["claims"]:
        lines.append(f"{item['tag']} [{item['period']}] {item['statement']}")
    return "\n".join(lines)


def parse_section_output(
    text: str, *, context: Mapping[str, Any], title: str
) -> dict[str, Any]:
    """Turn one model reply into a section the deliverable authority can check."""

    from .cockpit_model import unwrap_json_object

    parsed = unwrap_json_object(text) or {}
    body = parsed.get("body")
    if not isinstance(body, str) or not body.strip():
        return {"title": title, "body": "", "claim_refs": [], "numbers": [],
                "gaps": ["模型没有写出这一节的正文"]}
    claims_by_tag = {item["tag"]: item for item in context["claims"]}
    numbers_by_tag = {item["tag"]: item for item in context["numbers"]}
    claim_refs = [
        claims_by_tag[str(tag).strip()]["ref"]
        for tag in (parsed.get("claims") or []) if str(tag).strip() in claims_by_tag
    ]
    numbers = [
        {"text": numbers_by_tag[str(tag).strip()]["statement"],
         "claim_version_ref": numbers_by_tag[str(tag).strip()]["ref"],
         "period": numbers_by_tag[str(tag).strip()]["period"]}
        for tag in (parsed.get("numbers") or []) if str(tag).strip() in numbers_by_tag
    ]
    gaps = [str(gap)[:300] for gap in (parsed.get("gaps") or []) if str(gap).strip()][:20]
    # A tag the model invented is not evidence; the body keeps whatever it says
    # and the deliverable authority refuses any figure the cited numbers cannot
    # account for.  Reporting the drop here is what makes that visible.
    dropped = [
        str(tag) for tag in (parsed.get("claims") or [])
        if str(tag).strip() not in claims_by_tag
    ] + [
        str(tag) for tag in (parsed.get("numbers") or [])
        if str(tag).strip() not in numbers_by_tag
    ]
    if dropped:
        gaps.append(f"模型引用了不存在的标签：{'、'.join(dropped[:5])}")
    return {"title": title, "body": body.strip(), "claim_refs": claim_refs,
            "numbers": numbers, "gaps": gaps}


def assess_exit_gate(
    *,
    playbook: Mapping[str, Any],
    checklist_entry: Mapping[str, Any],
    sections: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """The Playbook's four questions, answered by structural checks only.

    Nothing here asks a model whether its own document is good.  Two questions
    are facts about the mission's source base and the publish-time number
    check; two are facts about which sections were actually written.
    """

    stage = next(
        (item for item in playbook.get("stages", ()) if item.get("stage_ref") == "initial_screen"),
        None,
    )
    questions = list((stage or {}).get("exit_gate", {}).get("questions") or [])
    written = {section["title"]: section for section in sections}
    def body_of(index: int) -> str:
        titles = list(written)
        return written[titles[index]]["body"] if index < len(titles) else ""

    missing_items = [
        item["label"] for item in checklist_entry.get("items", ())
        if item.get("status") != "complete"
    ]
    thesis_body = body_of(3)
    thesis_claims = len(sections[3]["claim_refs"]) if len(sections) > 3 else 0
    structural = [
        {
            "check": "source_base",
            "answer": not missing_items,
            "basis": "资料底座四项全部齐备" if not missing_items
                     else "还缺：" + "、".join(missing_items),
        },
        {
            "check": "number_provenance",
            "answer": True,
            "basis": "文档发布时逐条校验过：正文里的每个数字都绑定一条定量 Claim，否则拒绝发布",
        },
        {
            "check": "key_driver",
            "answer": len(thesis_body) >= 200 and thesis_claims >= 3,
            "basis": f"核心 thesis 一节写了 {len(thesis_body)} 字、引用了 {thesis_claims} 条结论"
                     + ("" if len(thesis_body) >= 200 and thesis_claims >= 3 else "，不足以认定已识别关键 driver"),
        },
        {
            "check": "street_and_risk",
            "answer": all(len(body_of(index)) >= 120 for index in (3, 4, 5)),
            "basis": "核心 thesis、风险与 anti-thesis、relevance to universe 三节都已写出"
                     if all(len(body_of(index)) >= 120 for index in (3, 4, 5))
                     else "thesis / 风险 / relevance 里还有没写出的部分",
        },
    ]
    answers = [
        {**item, "question": questions[index] if index < len(questions) else item["check"]}
        for index, item in enumerate(structural)
    ]
    passed = all(item["answer"] for item in answers)
    return {
        "schema_version": SCHEMA_VERSION,
        "passed": passed,
        "answers": answers,
        "rationale": (
            "四问全部为是，文档非空壳，数字零无源" if passed
            else "；".join(item["basis"] for item in answers if not item["answer"])
        ),
    }


__all__ = [
    "KIND",
    "SECTION_GUIDANCE",
    "TEMPLATE_KEY",
    "VALUATION_GAP",
    "assess_exit_gate",
    "build_claim_context",
    "build_section_prompt",
    "parse_section_output",
    "section_titles",
]
