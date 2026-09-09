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
# P10c/Q1: how many figures the drafter may offer. Live, every quantitative
# Claim in the Ledger is quarterly revenue growth, so a flat tail of the 40
# most recent rows was 40 revenue rows; see _select_numbers for how the budget
# is now shared out, and _carries_a_figure for what counts as a figure at all.
MAX_NUMBERS = 40
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


# C/N tags are scaffolding the prompt introduced, not reader-facing provenance
# -- the section's claim list is that -- and their digits would otherwise read
# as figures.  Stripped at parse time.
#
# P13ap: stripping them used to wreck the prose it left behind, and the wreckage
# reached published screens. The model writes tags as sentence constituents
# ("C12显示本季收入为…") and inside parenthetical source lists with Chinese
# labels ("（数据来源：N1、N2、N3）"), neither of which the group pattern matched,
# so the tags vanished and their punctuation stayed:
#
#   ；、、（同一季度数据重复）显示2025-09-01..
#   （数据来源：、、、）
#
# The real fix is the prompt below -- tags belong in the JSON arrays and never
# in the body. This is the second line of defence for text that still arrives
# with them, and it is deliberately conservative: it deletes what the tags left
# behind, and it does not attempt to rewrite a sentence back into grammar.
_BARE_TAG = r"(?<![A-Za-z0-9])[CN]\d{1,3}(?![A-Za-z0-9])"
_BARE_TAG_RE = re.compile(_BARE_TAG)
# Tags joined to each other -- "N1、N2、N3", "C1至C4", "C7和C8" -- are removed as
# one run rather than one at a time. Removing them individually left the joins
# behind, and "目前C1至C4显示" became "目前至显示".
_JOIN = r"(?:[、,，/;；·\s]|以及|和|与|及|至|到)"
_TAG_RUN_RE = re.compile(rf"{_BARE_TAG}(?:{_JOIN}+{_BARE_TAG})*")
# Any bracketed run, so a source list keeps its Chinese label while it is judged.
_BRACKET_RE = re.compile(r"[（(\[]([^（()）\[\]]*)[)）\]]")
_SEPARATORS = "、,，/;；·"
_SENTENCE_END = "。；！？：:!?"


def _has_content(text: str) -> bool:
    """Does this fragment still say anything, or is it only punctuation?"""

    return any(char.isalnum() for char in text)


def _drop_empty_brackets(text: str) -> str:
    """Delete a parenthetical whose contents the tag removal emptied.

    "（数据来源：N1、N2、N3）" becomes "（数据来源：）", which reads as a citation
    that lost its citations -- worse than no parenthetical at all, because it
    tells the reader something was there and does not say what.
    """

    def replace(match: re.Match[str]) -> str:
        inner = match.group(1)
        if not _has_content(inner):
            return ""
        # A label with nothing after it is an empty citation, whatever the
        # label says. Judge what follows the last colon, not the whole run.
        for colon in ("：", ":"):
            if colon in inner and not _has_content(inner.rsplit(colon, 1)[1]):
                return ""
        return match.group(0)

    return _BRACKET_RE.sub(replace, text)


def strip_citation_tags(text: str) -> str:
    """Remove the C/N citation scaffolding, and the holes it leaves."""

    cleaned = _TAG_RUN_RE.sub("", text or "")
    cleaned = _BARE_TAG_RE.sub("", cleaned)
    # A separator against a bracket edge was joining the tag to what is still
    # there: "（调整后，N3）" leaves "（调整后，）". Cleared before the bracket is
    # judged, so a parenthetical is only dropped when it is genuinely empty.
    cleaned = re.sub(f"[{_SEPARATORS}\\s]+([)）\\]])", r"\1", cleaned)
    cleaned = re.sub(f"([（(\\[])[{_SEPARATORS}\\s]+", r"\1", cleaned)
    cleaned = _drop_empty_brackets(cleaned)
    # A run of separators is what is left where a list of tags used to be.
    cleaned = re.sub(f"[{_SEPARATORS}][{_SEPARATORS}\\s]*", lambda m: m.group(0)[0], cleaned)
    # A separator with nothing before it -- at the start, or straight after a
    # sentence ended -- was joining tags to each other.
    cleaned = re.sub(f"^[{_SEPARATORS}\\s]+", "", cleaned)
    cleaned = re.sub(f"([{_SENTENCE_END}])[{_SEPARATORS}\\s]+", r"\1", cleaned)
    cleaned = re.sub(f"[{_SEPARATORS}]+([{_SENTENCE_END}])", r"\1", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\s+([，。、；：）)])", r"\1", cleaned)
    return cleaned.strip()


def section_titles(playbook: Mapping[str, Any]) -> list[str]:
    titles = (playbook.get("deliverable_templates") or {}).get(TEMPLATE_KEY) or []
    if not isinstance(titles, list) or not titles:
        raise ValueError("playbook carries no initial_screen template")
    return [str(title) for title in titles]


# P10c/Q1: which Claims came from the filing itself.  The Ledger's ``basis`` is
# free text written by whatever produced the Claim -- "official-filing-xbrl" for
# the SEC lane, "Earnings call commentary" or "JP Morgan Chime 2Q26 recap" for
# the rest -- so this reads it for the words that mean a filing rather than
# matching a closed vocabulary that does not exist yet.  Agent B's claim index
# will carry a real importance grade; until then this is the local reading.
_FILING_GRADE_RE = re.compile(
    r"filing|xbrl|10-?[kq]\b|20-?f\b|8-?k\b|annual report|prospectus|年报|季报|招股|公告",
    re.IGNORECASE,
)
_WHITESPACE_RE = re.compile(r"\s+")


def _is_filing_grade(claim: Mapping[str, Any]) -> bool:
    return bool(_FILING_GRADE_RE.search(str(claim.get("basis") or "")))


def _key_part(value: Any) -> str:
    """One component of a dedupe key, as something hashable.

    A Claim's ``period`` is usually a string but is not always one: live, one
    Accenture bookings Claim carries a structured period
    ({"kind": "fiscal_quarter", "label": "FY2026Q3", ...}).  A dict cannot go
    in a set, and the drafter crashing on a Ledger row is a worse failure than
    any duplicate it might have collapsed.
    """

    if value is None or isinstance(value, str):
        return "" if value is None else value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _normalised_statement(claim: Mapping[str, Any]) -> str:
    """The claim's words with spacing and terminal punctuation set aside."""

    text = _WHITESPACE_RE.sub(" ", str(claim.get("statement") or "")).strip()
    return text.rstrip("。.；;").lower()


def _by_preference(claims: Sequence[Mapping[str, Any]]) -> list[int]:
    """Row indices in the order the draft should prefer to cite them.

    Filing-grade provenance first, then the most recently recorded, then the
    ref, so the choice is total and does not depend on the order rows arrived.

    P13ap kept the *earliest* copy instead, to keep the cited ref stable across
    redrafts.  That argument is real and it loses to this one: a screen that
    cites a sell-side paraphrase of a number the filing itself states is citing
    the weaker of two sources, and the Playbook's source hierarchy is not a
    tie-break.  The cost -- a redraft can change which ref carries a fact when
    a better-sourced copy arrives -- is temporary; Agent B's claim index will
    name one canonical member per dedupe group and this preference retires
    with it.
    """

    order = sorted(range(len(claims)), key=lambda index: str(claims[index].get("ref") or ""))
    order.sort(key=lambda index: str(claims[index].get("created_at") or ""), reverse=True)
    order.sort(key=lambda index: 0 if _is_filing_grade(claims[index]) else 1)
    return order


def _dedupe(
    claims: Sequence[Mapping[str, Any]], keys: Any,
) -> tuple[list[dict[str, Any]], int]:
    """One claim per distinct assertion, the best-sourced copy kept.

    P13ap: three separate Claims asserted Accenture's 2025Q1 revenue in exactly
    the same words, so the drafter tagged them N1, N2 and N3 and the model --
    correctly, given what it was shown -- cited all three and noted the
    repetition in the published text: "、、（同一季度数据重复）显示…".

    The model was not wrong. The context was. A figure asserted three times is
    one figure, and offering it three times invites a sentence that says so.

    ``keys`` returns every identity a claim has, not one: two Claims are the
    same assertion if they say the same words *or* if they measure the same
    subject × metric × period × unit at the same value.  P13ap only had the
    first, so a filing's figure and a broker's restatement of it in different
    words were still offered side by side.  Assertions that genuinely differ --
    a restated figure, a different period -- share no key and are both kept;
    deciding between those is the Ledger's job, not the drafter's.
    """

    ordered = _by_preference(claims)
    seen: set[Any] = set()
    chosen: list[int] = []
    for index in ordered:
        identities = [key for key in keys(claims[index]) if key is not None]
        if not identities:
            chosen.append(index)
            continue
        if any(key in seen for key in identities):
            continue
        seen.update(identities)
        chosen.append(index)
    # Kept in the Ledger's own order, so the tags still read oldest first.
    return [dict(claims[index]) for index in sorted(chosen)], len(claims) - len(chosen)


def _number_keys(claim: Mapping[str, Any]) -> list[Any]:
    keys: list[Any] = _statement_keys(claim)
    aspect, period = claim.get("aspect"), claim.get("period")
    if aspect and period:
        keys.append((
            "measurement", _key_part(claim.get("subject_ref")), _key_part(aspect),
            _key_part(period), _key_part(claim.get("unit")), str(claim.get("value")),
        ))
    return keys


def _statement_keys(claim: Mapping[str, Any]) -> list[Any]:
    """Same words about the same period.

    The period qualifies the words because the Ledger's statements do not
    always carry one: two Claims that read "reported Revenues of USD 18.7bn"
    for two different quarters say the same thing about different things, and
    collapsing them would delete a figure rather than a duplicate.
    """

    statement = _normalised_statement(claim)
    return [("statement", _key_part(claim.get("period")), statement)] if statement else []


def _carries_a_figure(claim: Mapping[str, Any]) -> bool:
    """Does this Claim's own text assert a measurement?

    A Claim with a ``value`` is quantitative by construction, but a Claim
    without one can still state a figure in its sentence -- a utilisation rate
    from an earnings call, a segment number from an MD&A -- and the drafting
    contract lets a figure into the body only through an N tag.  Offered as a
    statement, such a Claim's number is unusable; offered as a figure, it is
    citable and checkable at publish time like any other.  This is what widens
    the numeric context beyond the one series the Ledger happens to type.
    """

    return bool(value_tokens(str(claim.get("statement") or "")))


def _select_numbers(
    claims: Sequence[Mapping[str, Any]], *, limit: int = MAX_NUMBERS
) -> list[dict[str, Any]]:
    """Latest first within each series, round-robin across series.

    A flat tail takes the most recently recorded rows, and when one series is
    written far more often than the others -- which is exactly what a lane does
    -- the most recent rows are all of that one series.  Live, every
    quantitative Claim in the Ledger is quarterly revenue growth, and every
    published screen has one numeric series; the tail was not the cause of that
    but it would have been the cause of the next one.  Round-robin means a
    second series is visible from its first row rather than from its fortieth,
    and a single series still fills the whole budget.
    """

    series: dict[str, list[Mapping[str, Any]]] = {}
    for claim in claims:
        series.setdefault(_key_part(claim.get("aspect")), []).append(claim)
    for rows in series.values():
        rows.sort(key=lambda claim: _key_part(claim.get("period")), reverse=True)
    chosen: list[Mapping[str, Any]] = []
    for round_index in range(limit):
        added = False
        for key in sorted(series):
            rows = series[key]
            if round_index < len(rows) and len(chosen) < limit:
                chosen.append(rows[round_index])
                added = True
        if not added or len(chosen) >= limit:
            break
    chosen.sort(key=lambda claim: (_key_part(claim.get("aspect")), _key_part(claim.get("period"))))
    return [dict(claim) for claim in chosen]


def build_claim_context(
    claims: Sequence[Mapping[str, Any]], *, max_claims: int = MAX_CLAIMS_PER_SECTION
) -> dict[str, Any]:
    """Tag the company's live Claims as C1..Cn (qualitative) and N1..Nk (figures)."""

    qualitative, quantitative = [], []
    for claim in claims:
        numeric = claim.get("value") is not None or _carries_a_figure(claim)
        (quantitative if numeric else qualitative).append(claim)
    # A figure is identified by what it asserts, not by which row carries it.
    quantitative, dropped_numbers = _dedupe(quantitative, _number_keys)
    qualitative, dropped_claims = _dedupe(qualitative, _statement_keys)
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
    for claim in _select_numbers(quantitative, limit=MAX_NUMBERS):
        tagged_numbers.append({
            "tag": f"N{len(tagged_numbers) + 1}", "ref": claim["ref"],
            "statement": str(claim.get("statement") or ""),
            "figures": value_tokens(str(claim.get("statement") or "")),
            "period": claim.get("period"), "aspect": claim.get("aspect"),
        })
    return {
        "claims": tagged_claims, "numbers": tagged_numbers,
        # Reported rather than silently absorbed: a company whose Claims
        # collapse heavily has a Ledger problem, and the drafter is the first
        # place that shows.
        "duplicates_dropped": {
            "claims": dropped_claims, "numbers": dropped_numbers,
        },
        # Which series the draft could see. One entry means the screen it
        # writes will have one numeric series however well it is written.
        "series": sorted({str(item.get("aspect") or "") for item in tagged_numbers}),
    }


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
        "- Use ONLY the tagged material below.",
        # P13ap: the tags are scaffolding, and a body that uses them as words
        # cannot survive their removal. Published screens carried "（数据来源：、、、）"
        # and sentences that began "显示本季收入为…" with no subject, because the
        # model had written "N1、N2、N3" and "C12显示…".
        "- NEVER write a C or N tag inside the body text -- not as a word, not in",
        "  brackets, not in a source list. List them in the JSON arrays instead;",
        "  that is what carries provenance to the reader. A sentence whose subject",
        "  is a tag becomes a sentence with no subject once the tag is removed, so",
        "  name the source in words: 管理层、该季报、卖方研报.",
        "- You may write a figure ONLY by citing the N tag that carries it, and the figure must appear",
        f"  in that N tag's text VERBATIM.  Do not convert units or scales (no 亿/万 rewriting, no",
        f"  rounding, no percentage recomputation): copy the digits exactly as the N tag prints them.",
        f"  If you need a number you do not have, write {GAP_MARKER}.",
        "- Do not number your paragraphs or write ordered lists; write prose.",
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
    if isinstance(body, str):
        body = strip_citation_tags(body)
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
    "strip_citation_tags",
    "SECTION_GUIDANCE",
    "TEMPLATE_KEY",
    "VALUATION_GAP",
    "assess_exit_gate",
    "build_claim_context",
    "build_section_prompt",
    "parse_section_output",
    "section_titles",
]
