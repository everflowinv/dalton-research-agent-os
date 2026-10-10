"""P10c: draft the Playbook's Initial Screen from Claims the Ledger holds.

The Playbook froze the document's eight sections and the four questions its
gate asks.  This module turns those into a bounded drafting pass:

- the context is the company's own live Claims, tagged ``C1..Cn``, plus its
  quantitative Claims tagged ``N1..Nk`` with the exact figure text;
- the model writes one section at a time and may cite only those tags.  A
  figure may appear in the body only if the section cites the ``N`` tag that
  carries it, so the number discipline is structural, not a request;
- the valuation section is drafted from the valuation snapshot when this Core
  holds one, tagged ``N`` like every other figure and citing the snapshot cell
  rather than a Claim, because a multiple is computed and no Claim will ever
  carry it; with no snapshot the section is still not drafted at all, because
  the Playbook says an unsourced number is worse than a gap;
- the exit gate is assessed by structural checks over the mission's own source
  base and the document that was written, never by asking a model whether its
  own work is good.

Nothing here writes: it produces the record the deliverable authority checks
and the gate assessment the stage ledger records.
"""

from __future__ import annotations

from .final_text_contract import final_text_instructions

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
    # P11c-E: drafted only when the valuation snapshot supplied N rows; the
    # caller falls back to VALUATION_GAP when it did not. The guidance is
    # deliberately about what the held multiples do and do not settle: this
    # Core computes trailing multiples and their own history, and holds no
    # target price and no forward multiple at all.
    "写估值：用已持有的估值快照说明这家公司现在被定价成什么样——哪几个倍数算得出来、"
    "每个倍数在自己历史区间的什么位置、以及这个位置是被价格推动的还是被申报基本面推动的。"
    "只能写 N 标签里逐字出现的数字；快照没给出的倍数就明说缺哪一项输入，不要用别的科目替代，"
    "也不要跨公司套用。有前瞻 EPS 与倍数时写观察价（倍数 × EPS = 观察价）、现价与隐含空间；"
    "估值链不可得时写「观察价：暂无法给出（原因）」并列出一致预期与卖方目标价作参照。"
    "如果我们读出来的估值与市场预期方向一致，直说这里没有差异化观点——共识一致等于没有观点，"
    "把它写成观点是这一节最容易犯的错。",
    "写数据跟踪：应该盯住哪些可观察的高频或定期数据来验证或证伪上面的判断。",
)
#: How much of one prior section the drafter is shown. Enough to judge, not
#: enough to copy comfortably, and bounded so a long old memo cannot crowd out
#: the Claims the new version has to be written from.
MAX_PRIOR_SECTION_CHARS = 1200
#: Why the valuation section is empty when it is empty. Accurate as of P11c-E:
#: the inputs are no longer the problem -- the price series, the share
#: observation and the filed statement lines are all held -- so the only honest
#: reason left is that this company has no published valuation snapshot yet.
#: The old text blamed six absent authorities and would now be a false
#: statement about this Core on every company the snapshot lane has priced.
VALUATION_GAP = (
    "估值一节按 Playbook 的数字纪律留空：这家公司还没有已发布的估值快照（"
    "valuation_snapshot_versions 里没有它的版本），本次冻结输入里因此没有任何可引用的"
    "倍数、市值或历史分位。系统其他流程可能已持有价格与申报数据，但本调用看不到的内容"
    "不能当作证据；等估值快照车道为这家公司发出第一版之后，这一节会自动改为据此起草。"
)
GATE_QUESTION_CHECKS = ("source_base", "number_provenance", "key_driver", "street_and_risk")
#: What one gate item can say about itself. ``passed`` / ``failed`` are the
#: structural checks below; ``imported`` is W3's third word and means the item
#: was never checked because the document was not written here -- it is a
#: prior screen brought in as v0, and answering the fund's exit gate about
#: somebody's 2024 document would be a fabrication either way it came out.
GATE_ITEM_STATUSES = ("passed", "failed", "imported")
#: The standing instruction the drafter is given when there is a prior version.
#: The whole of W3 in three sentences: read it, judge it, do not copy it.
PRIOR_REFERENCE_INSTRUCTION = (
    "上一版是我们自己以前的判断，不是事实，也不是本版的结论。逐条处理它的关注点与 debate："
    "仍然成立 / 已经变了 / 已经有答案，每一条都要用下面的 C、N 材料说明依据；"
    "只保留仍影响结论的问题，其余不再列出。"
    "不要照抄上一版的结论、措辞或数字——上一版的数字没有在本系统里核对过，"
    "本版的每个数字只能来自 N 标签。"
)


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


def raw_section_body(text: str) -> str:
    """The body the model wrote, before the citation scaffolding is stripped.

    The drafter needs both halves to tell wreckage from prose: a sentence that
    reads the same before and after the tags come out was never holding a tag.
    """

    from .cockpit_model import unwrap_json_object

    body = (unwrap_json_object(text) or {}).get("body")
    return body if isinstance(body, str) else ""


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
    claims: Sequence[Mapping[str, Any]], *,
    max_claims: int = MAX_CLAIMS_PER_SECTION,
    valuation: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Tag the company's live Claims as C1..Cn (qualitative) and N1..Nk (figures).

    ``valuation`` is P11c-E's computed layer: the multiples a published
    valuation snapshot holds, in the shape ``deep_insight_gate_cli``
    already mints them. They are tagged ``N`` with everything else, because
    from the drafter's side a figure is a figure and a second vocabulary
    would be a second way to get the number discipline wrong -- but they
    carry a ``cell`` instead of a Claim ref, since no Claim will ever carry
    a computed multiple.

    They are appended *after* the Claim figures rather than competing with
    them for ``MAX_NUMBERS``: there are at most four of them, they are the
    only figures the valuation section can be written from at all, and
    letting a flat tail of revenue rows crowd them out would leave that
    section with nothing for the sake of one more quarter of revenue.
    """

    qualitative, quantitative = [], []
    for claim in claims:
        numeric = claim.get("value") is not None or _carries_a_figure(claim)
        (quantitative if numeric else qualitative).append(claim)
    # A figure is identified by what it asserts, not by which row carries it.
    quantitative, dropped_numbers = _dedupe(quantitative, _number_keys)
    qualitative, dropped_claims = _dedupe(qualitative, _statement_keys)
    selected = _select_numbers(quantitative, limit=MAX_NUMBERS)
    # A Claim that carries a figure but did not win a place in the figure
    # budget is still something the company said, and it used to fall out of
    # the context entirely -- promoted out of the statements and then cut from
    # the numbers. It goes back into the statements, where it can still be
    # cited for what it asserts even though its figure cannot be written down.
    # Oldest first, because that is the order the tags read in.
    chosen = {item["ref"] for item in selected}
    statements = sorted(
        qualitative + [claim for claim in quantitative if claim["ref"] not in chosen],
        key=lambda claim: str(claim.get("created_at") or ""),
    )
    tagged_claims, tagged_numbers = [], []
    budget = MAX_CONTEXT_CHARS
    for claim in statements[-max_claims:]:
        line = str(claim.get("statement") or "")
        if budget - len(line) < 0:
            break
        budget -= len(line)
        tagged_claims.append({
            "tag": f"C{len(tagged_claims) + 1}", "ref": claim["ref"], "statement": line,
            "period": claim.get("period"), "aspect": claim.get("aspect"),
            "created_at": claim.get("created_at"),
        })
    for claim in selected:
        tagged_numbers.append({
            "tag": f"N{len(tagged_numbers) + 1}", "ref": claim["ref"],
            "statement": str(claim.get("statement") or ""),
            "figures": value_tokens(str(claim.get("statement") or "")),
            "period": claim.get("period"), "aspect": claim.get("aspect"),
            "cell": None,
        })
    for row in valuation:
        ref = str(row.get("ref") or "")
        version_ref = ref.rsplit(":", 1)[0].split(":", 1)[-1] if ref else ""
        if not ref or not version_ref:
            continue
        statement = str(row.get("text") or "")
        tagged_numbers.append({
            "tag": f"N{len(tagged_numbers) + 1}", "ref": ref,
            "statement": statement,
            "figures": value_tokens(statement),
            "period": row.get("period"), "aspect": "valuation",
            # What this figure cites instead of a Claim. The shape is
            # ``mission_deliverable.validate_cell_citation``'s, checked there
            # and resolved against the snapshot at publish time.
            "cell": {"kind": "valuation_metric", "ref": ref,
                     "version_ref": version_ref},
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
    prior_reference: Mapping[str, Any] | None = None,
) -> str:
    """One section's prompt, and optionally the previous version beside it.

    ``prior_reference`` is W3's block: what this fund said about this company
    last time, with its date and its age, and the standing instruction to
    judge each prior question rather than restate it. It is appended last, so
    the hard rules above -- and in particular "a figure only by citing an N
    tag" -- are read before the prior text is, and it carries its own
    reminder that the prior document's numbers were never verified here.
    """

    lines = [
        "You are drafting one section of an equity research Initial Screen for a fund's own file.",
        "Write in Chinese, in full sentences, for a portfolio manager who knows the sector.",
        *final_text_instructions(),
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
        "  name the source in words using the kind shown by the tagged row (for example 管理层、公告、",
        "  已申报报表、卖方共识、内部预测). Do not call an internal forecast or vendor",
        "  consensus a company actual, and do not call the house view buy-side consensus.",
        "- You may write a figure ONLY by citing the N tag that carries it, and the figure must appear",
        f"  in that N tag's text VERBATIM.  Do not convert units or scales (no 亿/万 rewriting, no",
        f"  rounding, no percentage recomputation): copy the digits exactly as the N tag prints them.",
        f"  If you need a number you do not have, write {GAP_MARKER}.",
        "- Do not number your paragraphs or write ordered lists; write prose.",
        "- Do not invent company names, products, dates or numbers.  Do not repeat the section title.",
        "- Evidence-backed analysis is allowed: distinguish source fact from 判断/推断, cite",
        "  the premises, and state uncertainty. Do not merely concatenate quotations.",
        "- If the material cannot support this section, say so in one sentence and list",
        "  the exact missing document, metric, period, comparison, or existing research action.",
        "- Where the section asks for a judgement, choose the evidence-weighted current case",
        "  rather than ending with A也可能、B也可能. State the alternative trigger, its operating/",
        "  earnings/valuation impact when the supplied evidence supports it, the falsifier, and",
        "  the next observable item that would move the judgement. A view, a watch price and a",
        "  trade angle are allowed when the tagged figures support them; label them as our judgement.",
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
    lines.extend(render_prior_reference(prior_reference))
    return "\n".join(lines)


def render_prior_reference(prior_reference: Mapping[str, Any] | None) -> list[str]:
    """The 「上一版（内部，YYYY-MM）」 block, or nothing at all.

    Nothing at all when there is no prior version, and that absence is the
    normal case: most companies have no earlier file, and a heading that says
    "previous version: none" is noise the model has to read past.
    """

    if not prior_reference:
        return []
    as_of = str(prior_reference.get("as_of") or "")
    months = prior_reference.get("age_months")
    age = f"，距今约 {months} 个月" if isinstance(months, int) else ""
    lines = [
        "",
        f"上一版（内部，{as_of[:7]}{age}）——参考，不是结论：",
        PRIOR_REFERENCE_INSTRUCTION,
    ]
    for section in prior_reference.get("sections") or ():
        title = str(section.get("title") or "").strip()
        body = str(section.get("body") or "").strip()
        if not title and not body:
            continue
        lines.append(f"【{title}】{body[:MAX_PRIOR_SECTION_CHARS]}")
        gaps = [str(gap).strip() for gap in (section.get("gaps") or ()) if str(gap).strip()]
        if gaps:
            lines.append("  当时未决：" + "、".join(gaps[:5]))
    refs = [str(ref) for ref in (prior_reference.get("refs") or ())][:8]
    if refs:
        lines.append("上一版出处：" + "、".join(refs))
    return lines


def valuation_section_held(
    title: str, guidance: str, valuation: Sequence[Mapping[str, Any]],
) -> bool:
    """Whether the valuation section is left to :data:`VALUATION_GAP`.

    Gated on having something to write it from, not on being the valuation
    section. Before P11c-E the answer was "always", because the guidance was
    empty; now it is "only when this Core has not priced the company", and a
    drafted-but-figureless valuation section -- which is what would happen if
    this returned False with no rows -- would read as "we looked and it is not
    interesting" rather than "we have not priced this company".
    """

    if VALUATION_TITLE_HINT not in title:
        return False
    return not guidance or not valuation


def _number_entry(tagged: Mapping[str, Any]) -> dict[str, Any]:
    """One cited figure, citing whichever provenance it actually has.

    Exactly one of the two, never both: the deliverable authority refuses an
    entry that claims a Claim and a cell, because a figure with two
    provenances gets checked against the more convenient one.
    """

    entry: dict[str, Any] = {
        "text": tagged["statement"], "period": tagged.get("period"),
    }
    cell = tagged.get("cell")
    if cell:
        entry["cell"] = dict(cell)
    else:
        entry["claim_version_ref"] = tagged["ref"]
    return entry


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
        _number_entry(numbers_by_tag[str(tag).strip()])
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
    delta_vs_prior: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The Playbook's four questions, answered by structural checks only.

    Nothing here asks a model whether its own document is good.  Two questions
    are facts about the mission's source base and the publish-time number
    check; two are facts about which sections were actually written.

    ``delta_vs_prior`` is W3's fifth block and is deliberately **not** a fifth
    answer: it is reported, never a trigger. What changed since the previous
    version is the question the owner asked to see on every re-issue, and it
    is also a question whose right answer is often "nothing much" -- a gate
    that failed on a quiet quarter would be a gate that punishes an honest
    document. So it sits beside ``answers`` where ``all()`` never sees it.
    """

    if [section.get("title") for section in sections] == list(STRL_TITLES):
        # A document written under the STRL contract (an erratum re-assessing
        # one, for instance) is read by its own gate; its figures were checked
        # at publish, which is what a stored version is.
        return assess_strl_gate(playbook=playbook, checklist_entry=checklist_entry,
                                sections=sections, verdict={"verdict": "pass", "findings": []},
                                delta_vs_prior=delta_vs_prior)
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
        {**item, "question": questions[index] if index < len(questions) else item["check"],
         # The same fact as ``answer``, in the vocabulary an imported gate
         # also speaks. A reader comparing a v0 to a v1 needs one word list,
         # not a boolean here and a status there.
         "status": "passed" if item["answer"] else "failed"}
        for index, item in enumerate(structural)
    ]
    passed = all(item["answer"] for item in answers)
    return {
        "schema_version": SCHEMA_VERSION,
        "passed": passed,
        "answers": answers,
        "delta_vs_prior": None if delta_vs_prior is None
                          else json.loads(json.dumps(dict(delta_vs_prior), ensure_ascii=False)),
        "rationale": (
            "四问全部为是，文档非空壳，数字零无源" if passed
            else "；".join(item["basis"] for item in answers if not item["answer"])
        ),
    }


# ===========================================================================
# Batch D: the STRL-shaped Initial Screen contract
# ===========================================================================
#
# The benchmark is a human analyst's STRL Initial Screen (2026-06-12): a view
# with a watch price up front, business economics with numbers, the industry
# and competition, our forecast against consensus with the gap and the
# assumption it comes from, valuation as multiple x EPS = watch price, a trade
# angle, risks with falsifiers, what to track, and an honest appendix of what
# we do not know.  The old contract forbade the view, the price and the trade
# angle and fed the drafter number-free Claims; this one drafts from the
# material pack (batch C) in two steps -- one structured worksheet, then the
# whole document in one pass sharing it -- and verifies citations, arithmetic,
# presence of view / forecast gap / valuation, and the reasoning lint, instead
# of deleting sentences it could not source.

from .product_contract import (  # noqa: E402 - the contract half of this module
    GAP_RULES,
    MATERIAL_REQUEST_REF_RE,
    NUMBER_SOURCE_RULES,
    STANCES,
    TRIED_RE,
    VIEW_RULES,
    contract_fingerprint as product_contract_fingerprint,
    material_missing_count,
    tag_entry,
    tag_kind,
)

def _pack(pack: Any) -> Mapping[str, Any]:
    """A batch-C ``MaterialPack`` (or an already-normalised dict) as a dict."""

    from .product_contract import normalise_pack

    if isinstance(pack, Mapping) and "tags" in pack and "text" in pack and "record" in pack:
        return pack
    return normalise_pack(pack)


STRL_CONTRACT_VERSION = "initial-screen-contract:strl:1"
STRL_TEMPLATE_REF = f"contract:{STRL_CONTRACT_VERSION}"

#: (key, title, what the section must contain).  Order is the document's.
STRL_SECTIONS: tuple[tuple[str, str, str], ...] = (
    ("conclusion", "结论与观点",
     "第一句写立场（看多/看空/中性/观察 之一）与观察价（或「观察价：暂无法给出（原因）」并列一致预期与"
     "卖方目标价区间作参照），然后用一段话写核心判断：为什么、差异在哪、什么会证伪。"),
    ("business_model", "商业模式与经济性",
     "收入与利润的 mix（分部/产品/客户）、单位经济性（价格、成本结构、合同条款）、利润率驱动，"
     "每一条都带数字与出处。"),
    ("industry", "行业结构与竞争",
     "市场规模与增速、集中度与份额、竞争对手与公司所处位置、壁垒来源；份额判断给出独立证据。"),
    ("forecast_vs_consensus", "盈利预测 vs consensus",
     "FY+1、FY+2 的收入与 EPS：我们的估计 vs 一致预期，差异 %（写出算式），以及差异来自哪条假设；"
     "引用一致预期数据源。"),
    ("valuation", "估值与观察价",
     "倍数 × EPS = 观察价（写算式）；现价与隐含空间；倍数理由覆盖历史分位、同业与业务属性；"
     "估值链不可得时写「观察价：暂无法给出（原因）」并列一致预期与卖方目标价。"),
    ("trading", "交易角度",
     "预期差与胜率、拥挤度/持仓、过去财报反应、催化日历（日期 + 事件）。"),
    ("risks", "风险与证伪",
     "怎么会错：每条风险写清传导到收入/利润/估值的路径与可观察的证伪条件。"),
    ("tracking", "跟踪指标",
     "要盯的指标：频率、来源、阈值、读数往哪边走意味着什么。"),
    ("unknowns", "附录：我们不知道什么",
     "只保留仍影响结论的问题。每条缺口标明 company_not_disclosed（公司不披露）或 not_acquired"
     "（系统没取到）；not_acquired 必须引用 MaterialRequest 并写「已尝试：…」。"),
)
STRL_KEYS: tuple[str, ...] = tuple(key for key, _, _ in STRL_SECTIONS)
STRL_TITLES: tuple[str, ...] = tuple(title for _, title, _ in STRL_SECTIONS)
STRL_TITLE_OF: Mapping[str, str] = {key: title for key, title, _ in STRL_SECTIONS}

STRL_RULES: tuple[str, ...] = (
    *VIEW_RULES,
    *NUMBER_SOURCE_RULES,
    "原始数值精度与正文展示精度分开：N 标签原值不改。EPS等标量需要减少机器精度时，在该tag的numbers条目写figure为同单位一位或两位小数（Decimal ROUND_HALF_UP）；系统保存原值、展示值、来源和规则。该例外不是逐字引文，不可改单位、量级或任选近似；推导若用展示值，必须引用该经验证的numbers条目。",
    *GAP_RULES,
    "上一版只作参考：只保留仍影响结论的问题，不照抄上一版的结论、措辞或数字。",
    "同一个事实最多出现在两节；不要每节重述公司简介。",
    "标签只写进 JSON 数组（claims / numbers / sources），不要写进正文。",
)

#: Closed worksheet shape.  Step one of two: the analysis the document shares.
WORKSHEET_FIELDS = frozenset({
    "stance", "core_judgement", "watch_price", "multiple_rationale", "forecasts",
    "key_passages", "trade", "open_questions",
})
WATCH_PRICE_FIELDS = frozenset({
    "status", "value", "currency", "multiple", "multiple_basis", "eps", "eps_tag",
    "expression", "current_price", "current_price_tag", "implied_return_pct",
    "implied_expression", "reason", "reference_tags",
})
FORECAST_FIELDS = frozenset({
    "year", "metric", "ours", "ours_tag", "consensus", "consensus_tag", "diff_pct",
    "diff_expression", "driver", "status", "reason", "unit",
})
FORECAST_METRICS = ("revenue", "eps")


class InitialScreenContractError(ValueError):
    """A worksheet or a document breaks the STRL contract."""


def _arith(expression: str) -> float:
    import ast

    from .mission_deliverable import _DERIVATION_RE, _evaluate

    if not isinstance(expression, str) or not _DERIVATION_RE.fullmatch(expression.strip()):
        raise InitialScreenContractError(f"not a plain arithmetic expression: {expression!r}")
    return _evaluate(ast.parse(expression.strip(), mode="eval"))


def _matches(expression: str, result: Any) -> bool:
    try:
        value = _arith(expression)
    except Exception:  # noqa: BLE001 - an expression that does not compute does not match
        return False
    text = str(result).strip()
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", text):
        return False
    decimals = len(text.split(".", 1)[1]) if "." in text else 0
    return abs(value - float(text)) <= 0.5 * 10 ** -decimals + 1e-9


def _tag_text(pack: Mapping[str, Any], tag: str) -> str:
    entry = tag_entry(pack, tag) or {}
    return str(entry.get("text") or "")


def _digits_in(value: Any, text: str) -> bool:
    plain = str(value).replace(",", "").strip()
    flat = text.replace(",", "")
    variants = {plain}
    if "." in plain:
        variants.add(plain.rstrip("0").rstrip("."))
    return any(re.search(rf"(?<![\d.]){re.escape(v)}(?![\d])", flat) for v in variants if v)


def _is_our_model(entry: Mapping[str, Any] | None) -> bool:
    """An ``[O…]`` tag bound to our model, or an ``[N…]`` tag into ``/our_model``."""

    if not entry:
        return False
    if entry.get("kind") == "number":
        return str(entry.get("source_kind")) == "ours"
    return entry.get("kind") == "ours" and str(entry.get("ref") or "").startswith(
        ("assumption:", "our-ref:", "our:", "forecast-cell:", "forecast-model-version:",
         "house-view:", "pack-json:/our_model"))


def _eps_basis(root: Mapping[str, Any], parent: Any) -> tuple[str, str] | None:
    """An explicitly held definition; currency alone never defines EPS basis."""
    basis = parent.get("eps_basis") if isinstance(parent, Mapping) else None
    if basis is None: basis = root.get("eps_basis")
    if isinstance(basis, str):
        kind, definition = basis, ""
    elif isinstance(basis, Mapping):
        kind, definition = basis.get("kind") or basis.get("vendor"), basis.get("definition_ref") or ""
    else: return None
    if kind not in {"gaap", "adjusted", "core"}: return None
    # GAAP has a common reporting basis. Adjusted/core definitions require the
    # same independently held definition, not a shared marketing label.
    return str(kind), str(definition)


def _eps_comparable(ours: Mapping[str, Any] | None, consensus: Mapping[str, Any] | None) -> bool:
    if not ours or not consensus: return False
    a, b = ours.get("eps_basis"), consensus.get("eps_basis")
    return bool(a and b and a == b and (a[0] == "gaap" or a[1]))


def _forecast_identity(pack: Mapping[str, Any], entry: Mapping[str, Any] | None, *, ours: bool) -> dict[str, Any] | None:
    """A typed number pointer, never a debate paragraph or a bare assumption."""
    if not entry or entry.get("kind") != "number": return None
    pointer = str(entry.get("pointer") or "")
    parts = pointer.strip("/").split("/")
    data = pack.get("json") or {}
    if ours:
        model = (data.get("our_model") or {}).get("model") or {}
        if not pointer.startswith("/our_model/") or not str(model.get("version_ref") or "").startswith("forecast-model-version:"):
            return None
        root = data.get("our_model") or {}
    else:
        if entry.get("source_kind") != "consensus": return None
        # The pack repeats a consensus operand beside our annual estimate.
        # Resolve that exact copied operand back to its canonical period and
        # metric; its location under /our_model does not make it our forecast.
        comparison = re.fullmatch(r"/our_model/years/(\d+)/(revenue|eps)_vs_consensus/theirs", pointer)
        if comparison:
            try:
                annual = data["our_model"]["years"][int(comparison[1])]
                metric = comparison[2]
                canonical = data["numbers"]["consensus"]["periods"]
                matches = [(i, row) for i, row in enumerate(canonical) if row.get("label") == annual["label"]]
                if len(matches) != 1: return None
                i, source = matches[0]
                if source.get("status") != "available" or not isinstance(source.get(metric), Mapping): return None
                if annual[metric + "_vs_consensus"]["theirs"] != source[metric].get("mean"): return None
                if entry.get("period") != source["label"]: return None
                return _forecast_identity(pack, {**entry, "pointer": f"/numbers/consensus/periods/{i}/{metric}/mean"}, ours=False)
            except (KeyError, IndexError, TypeError, ValueError): return None
        if not pointer.startswith("/numbers/consensus/"): return None
        root = ((data.get("numbers") or {}).get("consensus") or {})
    metric = "eps" if "eps" in parts or "ntm_eps" in parts else "revenue" if "revenue" in parts else None
    if metric is None: return None
    period = entry.get("period") or ("NTM" if "ntm_eps" in parts and root.get("quarters") else None)
    if not period: return None
    # Currency/unit belongs to the underlying field. Display scaling is an
    # explicit transform, and two comparison operands must use the same scale.
    node: Any = data
    parent: Any = None
    try:
        for part in parts:
            parent = node
            node = node[int(part)] if isinstance(node, list) else node[part]
    except (KeyError, IndexError, TypeError, ValueError): return None
    currency = (parent.get("currency") if isinstance(parent, Mapping) else None) or root.get("currency")
    unit = (parent.get("unit") if isinstance(parent, Mapping) else None) or (root.get("units") or {}).get(metric)
    if unit is None and currency: unit = str(currency) + ("/shares" if metric == "eps" else "")
    if unit is None: return None
    canonical_units = {"usd": "USD", "usd_per_share": "USD/shares", "USD/share": "USD/shares"}
    unit = canonical_units.get(str(unit), str(unit))
    if ours and metric == "eps" and isinstance(parent, Mapping) and parent.get("method") in {"sum_of_four_quarters", "sum_next_four_forecast_quarters"}:
        return None  # Non-additive quarterly EPS is not annual EPS authority.
    return {"period": str(period), "metric": metric, "unit": str(unit), "transform": entry.get("transform") or "identity",
            "model_version_ref": (root.get("model") or {}).get("version_ref") if ours else None,
            "eps_basis": _eps_basis(root, parent) if metric == "eps" else None}


def validate_worksheet(worksheet: Any, pack: Mapping[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Step one's output, closed; returns ``(worksheet, findings)``.

    Deterministic: the stance is from the closed vocabulary; the watch price
    re-computes from its expression and its multiple and EPS are in it; the
    implied return re-computes; every forecast row's diff re-computes from
    ours and consensus, ours is an ``O`` tag (or a model assumption) and
    consensus is a consensus ``N`` tag carrying that figure; an unavailable
    watch price states a reason and lists reference tags.
    """

    pack = _pack(pack)

    findings: list[dict[str, Any]] = []
    if not isinstance(worksheet, Mapping) or set(worksheet) - WORKSHEET_FIELDS \
            or not {"stance", "core_judgement", "watch_price", "forecasts"} <= set(worksheet):
        raise InitialScreenContractError(
            f"worksheet_shape: extra={sorted(set(worksheet)-WORKSHEET_FIELDS) if isinstance(worksheet,Mapping) else []}; "
            f"missing={sorted(WORKSHEET_FIELDS-set(worksheet)) if isinstance(worksheet,Mapping) else sorted(WORKSHEET_FIELDS)}; "
            f"expected={sorted(WORKSHEET_FIELDS)}")
    sheet = json.loads(json.dumps(dict(worksheet), ensure_ascii=False))
    if sheet["stance"] not in STANCES:
        findings.append({"code": "stance_not_in_vocabulary", "detail": str(sheet["stance"])[:40]})
    if not str(sheet.get("core_judgement") or "").strip():
        findings.append({"code": "core_judgement_missing"})
    watch = sheet.get("watch_price")
    if not isinstance(watch, Mapping) or set(watch) - WATCH_PRICE_FIELDS:
        findings.append({"code": "watch_price_shape"})
    elif watch.get("status") == "given":
        expression = str(watch.get("expression") or "")
        if not _matches(expression, watch.get("value")):
            findings.append({"code": "watch_price_arithmetic",
                             "detail": f"{expression} != {watch.get('value')}"})
        if not _matches(f"{watch.get('multiple')}*{watch.get('eps')}", watch.get("value")):
            findings.append({"code": "watch_price_not_multiple_times_eps"})
        for key in ("multiple", "eps"):
            if not watch.get(key) or not _digits_in(watch[key], expression):
                findings.append({"code": f"watch_price_{key}_not_in_expression"})
        eps_tag = str(watch.get("eps_tag") or "")
        if tag_kind(eps_tag) not in {"ours", "number", "passage"} or tag_entry(pack, eps_tag) is None:
            findings.append({"code": "watch_price_eps_unsourced", "detail": eps_tag})
        elif not _digits_in(watch.get("eps"), _tag_text(pack, eps_tag)):
            findings.append({"code": "watch_price_eps_not_in_tag", "detail": eps_tag})
        eps_identity = _forecast_identity(pack, tag_entry(pack, eps_tag), ours=True)
        if not eps_identity or eps_identity["metric"] != "eps":
            findings.append({"code": "watch_price_eps_not_bound_to_held_model", "detail": eps_tag})
        else:
            basis = str(watch.get("multiple_basis") or "")
            periods = re.findall(r"FY\d{4}(?:Q[1-4])?|NTM", basis, re.IGNORECASE)
            if periods != [eps_identity["period"]]:
                findings.append({"code": "watch_price_eps_period_mismatch", "detail": eps_tag})
            if eps_identity["unit"] != str(watch.get("currency")) + "/shares":
                findings.append({"code": "watch_price_eps_currency_mismatch", "detail": eps_tag})
        price_tag = str(watch.get("current_price_tag") or "")
        if tag_entry(pack, price_tag) is None or not _digits_in(watch.get("current_price"),
                                                                 _tag_text(pack, price_tag)):
            findings.append({"code": "current_price_unsourced", "detail": price_tag})
        if not _matches(str(watch.get("implied_expression") or ""), watch.get("implied_return_pct")):
            findings.append({"code": "implied_return_arithmetic"})
        if not _matches(f"({watch.get('value')}/{watch.get('current_price')}-1)*100", watch.get("implied_return_pct")):
            findings.append({"code": "implied_return_not_over_watch_and_current"})
    elif watch.get("status") == "unavailable":
        if not str(watch.get("reason") or "").strip():
            findings.append({"code": "watch_price_unavailable_without_reason"})
        refs = [tag for tag in watch.get("reference_tags") or [] if tag_entry(pack, tag)]
        if not refs:
            findings.append({"code": "watch_price_unavailable_without_reference",
                             "detail": "列出一致预期与卖方目标价作参照"})
    else:
        findings.append({"code": "watch_price_status", "detail": str(watch.get("status"))[:20]})
    forecasts = sheet.get("forecasts") or []
    if not isinstance(forecasts, list):
        raise InitialScreenContractError("worksheet.forecasts must be a list")
    seen: set[tuple[str, str]] = set()
    years: list[str] = []
    for index, row in enumerate(forecasts):
        name = f"forecasts[{index}]"
        if not isinstance(row, Mapping) or set(row) - FORECAST_FIELDS:
            findings.append({"code": "forecast_shape", "detail": name})
            continue
        metric = str(row.get("metric") or "")
        year = str(row.get("year") or "")
        if metric not in FORECAST_METRICS or not year:
            findings.append({"code": "forecast_metric_or_year", "detail": name})
            continue
        seen.add((year, metric))
        if year not in years:
            years.append(year)
        unavailable = row.get("status") == "unavailable"
        if unavailable:
            if (not str(row.get("reason") or "").strip()
                    or any(row.get(key) is not None for key in ("diff_pct", "diff_expression"))):
                findings.append({"code": "forecast_unavailable_invalid", "detail": name})
        if not re.fullmatch(r"FY\d{4}", year):
            findings.append({"code": "forecast_not_annual_period", "detail": name})
        ours_tag = str(row.get("ours_tag") or "")
        ours_entry = tag_entry(pack, ours_tag) if tag_kind(ours_tag) else None
        ours_identity = _forecast_identity(pack, ours_entry, ours=True)
        if unavailable and row.get("ours") is None:
            ours_identity = None
        elif not ours_identity:
            findings.append({"code": "forecast_ours_not_bound_to_model", "detail": name})
        elif ours_entry is not None and not _digits_in(row.get("ours"), str(ours_entry.get("text") or "")):
            findings.append({"code": "forecast_ours_not_in_tag", "detail": name})
        consensus_tag = str(row.get("consensus_tag") or "")
        entry = tag_entry(pack, consensus_tag) or {}
        consensus_identity = _forecast_identity(pack, entry, ours=False)
        if unavailable and row.get("consensus") is None:
            consensus_identity = None
        elif str(entry.get("source_kind") or "") != "consensus":
            findings.append({"code": "forecast_consensus_not_consensus_authority", "detail": name})
        elif not _digits_in(row.get("consensus"), str(entry.get("text") or "")):
            findings.append({"code": "forecast_consensus_not_in_tag", "detail": name})
        for source, identity in (("ours", ours_identity), ("consensus", consensus_identity)):
            if unavailable and row.get(source) is None:
                continue
            if identity is None:
                findings.append({"code": f"forecast_{source}_identity_missing", "detail": name})
            elif identity["period"] != year or identity["metric"] != metric:
                findings.append({"code": f"forecast_{source}_period_or_metric_mismatch", "detail": name})
        if ours_identity and consensus_identity and (ours_identity["unit"], ours_identity["transform"]) != (consensus_identity["unit"], consensus_identity["transform"]):
            findings.append({"code": "forecast_unit_or_scale_mismatch", "detail": name})
        basis_gap = metric == "eps" and ours_identity and consensus_identity and not _eps_comparable(ours_identity, consensus_identity)
        both_known = all(row.get(key) is not None for key in ("ours", "consensus"))
        if basis_gap and not unavailable:
            findings.append({"code": "forecast_eps_basis_not_comparable", "detail": name + ": held EPS definitions missing or unequal"})
        if unavailable and both_known and not (basis_gap and re.search(r"EPS|口径|basis", str(row.get("reason") or ""), re.I)):
            findings.append({"code": "forecast_unavailable_invalid", "detail": name + ": both known operands require a proved basis gap"})
        if row.get("unit") and ours_identity and str(row["unit"]) != ours_identity["unit"]:
            findings.append({"code": "forecast_declared_unit_mismatch", "detail": name})
        if unavailable:
            continue
        if not _matches(str(row.get("diff_expression") or ""), row.get("diff_pct")):
            findings.append({"code": "forecast_diff_arithmetic", "detail": name})
        else:
            expression = str(row.get("diff_expression"))
            if not (_digits_in(row.get("ours"), expression) and _digits_in(row.get("consensus"), expression)):
                findings.append({"code": "forecast_diff_not_over_ours_and_consensus", "detail": name})
        if not _matches(f"({row.get('ours')}/{row.get('consensus')}-1)*100", row.get("diff_pct")):
            findings.append({"code": "forecast_diff_not_standard_comparison", "detail": name})
        if not str(row.get("driver") or "").strip():
            findings.append({"code": "forecast_driver_missing", "detail": name})
    for year in years[:2]:
        for metric in FORECAST_METRICS:
            if (year, metric) not in seen:
                findings.append({"code": "forecast_missing", "detail": f"{year} {metric}"})
    if len(years) < 2:
        findings.append({"code": "forecast_needs_two_years", "detail": "FY+1 与 FY+2"})
    for tag in sheet.get("key_passages") or []:
        if tag_kind(str(tag)) != "passage" or tag_entry(pack, str(tag)) is None:
            findings.append({"code": "key_passage_unknown", "detail": str(tag)[:20]})
    return sheet, findings


def build_worksheet_prompt(
    *, company: Mapping[str, Any], pack: Mapping[str, Any],
    prior_reference: Mapping[str, Any] | None = None,
) -> str:
    """Step one: the structured analysis worksheet (view, forecasts, valuation, passages)."""

    pack = _pack(pack)

    contract = {
        "stance": "|".join(STANCES),
        "core_judgement": "一段话的核心判断",
        "watch_price": {
            "status": "given|unavailable", "value": "85.0", "currency": "USD",
            "multiple": "17.0", "multiple_basis": "FY+2 P/E", "eps": "5.00", "eps_tag": "N1",
            "expression": "17.0*5.00", "current_price": "80.00", "current_price_tag": "N3",
            "implied_return_pct": "6.3", "implied_expression": "(85.0/80.00-1)*100",
            "reason": "only when unavailable", "reference_tags": ["N6"],
        },
        "multiple_rationale": {"history": "...", "peers": "...", "business": "..."},
        "forecasts": [{"year": "FY2027", "metric": "revenue|eps", "ours": "5.00", "ours_tag": "N1",
                       "consensus": "4.50", "consensus_tag": "N2", "diff_pct": "11.1",
                       "diff_expression": "(5.00/4.50-1)*100", "driver": "差异来自哪条假设"}],
        "key_passages": ["P1"],
        "trade": {"edge": "...", "crowding": "...", "earnings_reaction": "...",
                  "catalysts": [{"date": "YYYY-MM-DD", "event": "..."}]},
        "open_questions": [{"question": "...", "affects_conclusion": True,
                            "gap_kind": "company_not_disclosed|not_acquired",
                            "request_ref": "material-request:… (not_acquired only)"}],
    }
    lines = [
        "You are the analyst writing an Initial Screen in the shape of a senior human analyst's "
        "screen. Step 1 of 2: fill the analysis worksheet. Do not write the document yet.",
        "Write the free-text fields in Chinese.",
        "",
        "Rules:",
        *[f"- {rule}" for rule in VIEW_RULES],
        "- This is a typed worksheet, not the document. Do not add a top-level numbers array or any other unlisted field. "
        "Use only the existing *_tag fields and reference_tags/key_passages to bind numbers to the pack. "
        "Arithmetic belongs in diff_expression, expression and implied_expression; retain exact period, metric, unit and held-model identity. "
        "Do not convert units, guess a missing value, or cite a hypothetical/debate value as a held-model result.",
        "- forecasts: FY+1 and FY+2 annual revenue and EPS (four rows). Numeric ours_tag must be an N number "
        "pointer into /our_model bound to a held forecast-model-version, with the exact fiscal year, metric, currency/unit and display scale. "
        "O debate/thesis tags and newly invented assumption: refs are not numeric forecast authority. consensus_tag must be an N consensus number "
        "for that exact annual FY and metric in the same unit/scale. Never compare annual with quarterly or reuse FY+1 as FY+2. "
        "EPS comparisons also require explicit held EPS definitions: GAAP may compare only with GAAP; adjusted/core require the same held definition_ref. Unknown basis or GAAP versus adjusted is not comparable even if currency/year match. For a basis gap retain both known cited operands, set status=unavailable, explain the EPS basis gap and set diff_pct/diff_expression=null. Never attribute that cross-basis numerical spread to business drivers. diff_pct=(ours/consensus-1)*100 only for comparable available operands. If either is missing, set row status=unavailable, "
        "reason=the exact gap, set only the missing side (ours or consensus) to null, retain and cite the known side, and set diff_pct/diff_expression=null; no manufactured zero or percent. A narrative hypothesis may be described in driver, not passed off as a held forecast.",
        "- watch_price.expression is multiple*EPS with the digits exactly as written in multiple and eps;"
        " implied_expression is (watch/current-1)*100.",
        "- Given watch_price requires an exact held-model EPS N tag and an explicit matching FY or NTM in multiple_basis. "
        "No unsourced assumption EPS, consensus EPS, debate O tag, or company guidance can stand in for our held-model EPS. "
        "Without held-model forward EPS for that exact period, watch_price must be unavailable even if a hypothetical EPS was discussed.",
        "- If the valuation chain is unavailable (no forward EPS or no defensible multiple), set "
        "status=unavailable, give the reason, and list consensus / sell-side target tags in reference_tags.",
        "- key_passages: the P tags the document must rest on (latest transcript and release first).",
        "- open_questions: only questions that still affect the conclusion.",
        "",
        "Return raw JSON only, exactly this shape:",
        json.dumps(contract, ensure_ascii=False),
        "",
        f"Company: {company.get('ticker')} ({company.get('company_ref')})",
        f"Material pack (recipe {pack.get('recipe_version')}, hash {str(pack.get('content_hash'))[:16]}):",
        str(pack.get("text") or ""),
    ]
    lines.extend(render_prior_reference(prior_reference))
    return "\n".join(lines)


def build_document_prompt(
    *, company: Mapping[str, Any], pack: Mapping[str, Any], worksheet: Mapping[str, Any],
    prior_reference: Mapping[str, Any] | None = None, repair: Sequence[Mapping[str, Any]] = (),
) -> str:
    """Step two: the whole document in one pass, sharing the worksheet."""

    pack = _pack(pack)

    from .research_reasoning_lint import VERIFIER_LINT_INSTRUCTIONS

    contract = {"sections": [
        {"key": key, "body": "正文（中文，分段）", "claims": ["C1"],
         "sources": ["P1", "N2"],
         "numbers": [
             {"tag": "P1", "quote": "原文中含该数字的片段", "metric": "指标", "period": "期间", "unit": "单位"},
             {"tag": "N2", "figure": "15.90", "metric": "指标", "period": "期间", "unit": "单位"},
             {"derivation": {"expression": "(5.00/4.50-1)*100", "result": "11.1"},
              "text": "正文中写出结果的片段 11.1%", "metric": "指标", "period": "期间", "unit": "%"},
             {"estimate": "O2", "text": "我们的估计 … 5.00", "metric": "指标", "period": "期间", "unit": "单位"},
         ],
         "gap_items": [{"kind": "company_not_disclosed|not_acquired", "need": "缺什么",
                        "request_ref": "material-request:…", "tried": "已尝试：…"}]}
        for key in STRL_KEYS
    ]}
    lines = [
        "You are the analyst writing an Initial Screen. Step 2 of 2: write the whole document in "
        "one pass from the worksheet below and the material pack. Every section, in this order:",
        *[f"- {key} 「{title}」：{guidance}" for key, title, guidance in STRL_SECTIONS],
        "",
        *final_text_instructions(),
        "",
        "Contract rules:",
        *[f"- {rule}" for rule in STRL_RULES],
        "",
        "Reasoning rules (a verifier rejects the document on any of these):",
        *[f"- {rule}" for rule in VERIFIER_LINT_INSTRUCTIONS],
        "",
        "The worksheet is binding: the stance, watch price, forecasts and their differences in the "
        "document must retain the worksheet's original numeric bindings. A same-unit one- or two-decimal "
        "display may use the declared numbers[].figure rule; keep the original value in the source "
        "and cite the validated display entry when arithmetic uses that display value. This is not a verbatim quote.",
        "EPS comparison unavailable for a basis gap means no numeric ours-versus-consensus EPS spread or implied driver reconciliation. Keep each independently known EPS and its basis separate; do not compare held-model EPS to adjusted guidance without a proved common definition.",
        "Worksheet:",
        json.dumps(worksheet, ensure_ascii=False, sort_keys=True),
        "",
        "Return raw JSON only, exactly this shape (one entry per section key, in order):",
        json.dumps(contract, ensure_ascii=False),
        "",
        f"Company: {company.get('ticker')} ({company.get('company_ref')})",
        *_open_request_lines(pack),
        "Material pack:",
        str(pack.get("text") or ""),
    ]
    lines.extend(render_prior_reference(prior_reference))
    if repair:
        lines += [
            "",
            "The verifier rejected your previous document for these findings. Rewrite the whole "
            "document fixing every one of them; keep everything else:",
            *[f"- {item.get('code')}: {item.get('section') or ''} {item.get('excerpt') or item.get('detail') or ''}"
              for item in list(repair)[:30]],
        ]
    return "\n".join(lines)


def _open_request_lines(pack: Mapping[str, Any]) -> list[str]:
    """The MaterialRequests the sufficiency check opened, for the appendix to cite."""

    sufficiency = pack.get("sufficiency") or {}
    refs = [str(ref) for ref in sufficiency.get("gap_request_refs") or ()]
    if not refs:
        return []
    missing = [str(item.get("key") if isinstance(item, Mapping) else item)
               for item in sufficiency.get("missing") or ()]
    return ["Material still missing (cite these MaterialRequests in 附录 and wherever the gap matters; "
            f"status {sufficiency.get('status')}): "
            + "；".join(f"{name} → {ref}" for name, ref in zip(missing + [""] * len(refs), refs))]


def _source_ref_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    out = {"ref": str(entry["ref"]), "tag": str(entry["tag"]),
           "source_kind": str(entry.get("source_kind") or entry["kind"])}
    for key in ("as_of", "company_ref", "title", "doc_kind", "document_ref"):
        if entry.get(key):
            out[key] = str(entry[key]) if key in ("doc_kind", "document_ref") else str(entry[key])[:200]
    if entry.get("company_refs"):
        # These are the pack's document associations, not proof that a cited
        # sentence belongs to any one of them. Keep the exact list and avoid
        # sharing a mutable list with the frozen pack.
        out["company_refs"] = list(entry["company_refs"]) if isinstance(entry["company_refs"], list) else entry["company_refs"]
    return out


def _normalise_space(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def parse_document_output(
    value: Any, *, pack: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Step two's reply as deliverable sections, plus contract findings.

    A tag the pack does not hold, a quote that is not in its passage, or an
    estimate not bound to our model is a finding, never silently dropped: the
    verifier rejects the document and the repair is told exactly which.
    """

    pack = _pack(pack)

    from .product_contract import ProductContractError, validate_gap_item

    findings: list[dict[str, Any]] = []
    if not isinstance(value, Mapping) or not isinstance(value.get("sections"), list):
        raise InitialScreenContractError("the document must be {\"sections\": [...]}")
    raw = value["sections"]
    keys = [str(item.get("key")) if isinstance(item, Mapping) else "" for item in raw]
    if keys != list(STRL_KEYS):
        raise InitialScreenContractError(
            f"the document must have exactly the sections {list(STRL_KEYS)} in order, got {keys}")
    sections: list[dict[str, Any]] = []
    for item in raw:
        key = str(item["key"])
        title = STRL_TITLE_OF[key]
        body = strip_citation_tags(str(item.get("body") or "")).strip()
        claim_refs: list[str] = []
        sources: dict[str, dict[str, Any]] = {}
        for tag in [*(item.get("claims") or []), *(item.get("sources") or [])]:
            entry = tag_entry(pack, str(tag))
            if entry is None:
                findings.append({"code": "unknown_tag", "section": title, "detail": str(tag)[:20]})
                continue
            if entry["kind"] == "claim":
                claim_refs.append(entry["ref"])
            sources.setdefault(entry["ref"], _source_ref_entry(entry))
        numbers: list[dict[str, Any]] = []
        for number in item.get("numbers") or []:
            if not isinstance(number, Mapping):
                findings.append({"code": "number_shape", "section": title})
                continue
            meta = {k: str(number[k])[:120] for k in ("metric", "period", "unit") if number.get(k)}
            if number.get("derivation") is not None:
                numbers.append({"text": str(number.get("text") or ""),
                                "derivation": dict(number["derivation"]), **meta})
                continue
            if number.get("estimate") is not None:
                ref = str(number["estimate"])
                entry = tag_entry(pack, ref) if tag_kind(ref) else None
                if entry is not None:
                    if not _is_our_model(entry):
                        findings.append({"code": "estimate_not_our_model", "section": title, "detail": ref})
                        continue
                    sources.setdefault(entry["ref"], _source_ref_entry(entry))
                    ref = entry["ref"]
                text = str(number.get("text") or (entry or {}).get("text") or "")
                numbers.append({"text": text, "estimate": {"assumption_ref": ref}, **meta})
                continue
            tag = str(number.get("tag") or "")
            entry = tag_entry(pack, tag)
            if entry is None:
                findings.append({"code": "unknown_tag", "section": title, "detail": tag[:20]})
                continue
            displayed = None
            exact_figure = False
            figure = str(number.get('figure') or '').strip()
            if figure and entry['kind']=='number' and not entry.get('cell'):
                from .mission_deliverable import number_display, MissionDeliverableError, _display_tokens
                raw_text=str(entry.get('text') or '')
                if figure != raw_text:
                    exact_figure = figure in {token.lstrip('$€£¥').rstrip('%') for token in _display_tokens(raw_text)}
                    if not exact_figure:
                        try:displayed=number_display(raw_text,figure)
                        except MissionDeliverableError as exc:
                            # Refuse the whole document; retain its body and raw source.
                            findings.append({'code':'number_display_invalid','section':title,'detail':str(exc)[:200]})
            if _is_our_model(entry):
                # Our model's figure cited as a plain tag is still 「我们的估计」:
                # recorded as one, so the prose must say so.
                sources.setdefault(entry["ref"], _source_ref_entry(entry))
                numbers.append({"text": str(number.get("text") or (figure if displayed else entry.get("text")) or ""),
                                "estimate": {"assumption_ref": entry["ref"]}, **meta,
                                **({'display':displayed} if displayed else {})})
                continue
            sources.setdefault(entry["ref"], _source_ref_entry(entry))
            for key_ in ("metric", "period", "unit"):
                if key_ not in meta and entry.get(key_):
                    meta[key_] = str(entry[key_])[:120]
            if entry["kind"] == "passage":
                quote = str(number.get("quote") or "").strip()
                passage = str(entry.get("text") or "")
                if quote and _normalise_space(quote) not in _normalise_space(passage):
                    findings.append({"code": "quote_not_in_passage", "section": title,
                                     "detail": f"{tag}: {quote[:60]}"})
                    continue
                numbers.append({"text": quote or passage[:1000], "passage_ref": entry["ref"], **meta})
            elif entry["kind"] == "number":
                text = figure if displayed else str(entry.get("text") or "")
                if entry.get("cell"):
                    numbers.append({"text": text, "cell": dict(entry["cell"]), **meta})
                else:
                    numbers.append({"text": text, "number_ref": entry["ref"], **meta,
                                    **({'display':displayed} if displayed else {})})
            elif entry["kind"] == "claim":
                numbers.append({"text": str(entry.get("text") or ""),
                                "claim_version_ref": entry["ref"], **meta})
                claim_refs.append(entry["ref"])
            else:
                numbers.append({"text": str(number.get("text") or entry.get("text") or ""),
                                "estimate": {"assumption_ref": entry["ref"]}, **meta})
        gap_items = []
        for index, gap in enumerate(item.get("gap_items") or []):
            try:
                gap_items.append(validate_gap_item(gap, f"{title}.gap_items[{index}]"))
            except ProductContractError as exc:
                findings.append({"code": "gap_item_invalid", "section": title, "detail": str(exc)[:160]})
        section = {"title": title, "body": body, "claim_refs": list(dict.fromkeys(claim_refs)),
                   "numbers": numbers, "gaps": [], "source_refs": list(sources.values())}
        if gap_items:
            section["gap_items"] = gap_items
        sections.append(section)
    return sections, findings


_STANCE_RE = re.compile("(?:" + "|".join(STANCES) + ")(?!价)")
_WATCH_GIVEN_RE = re.compile(r"观察价[^。；\n]{0,20}?\d")
_WATCH_UNAVAILABLE_RE = re.compile(r"观察价[：:]\s*暂无法给出（[^）]{2,}）")
_PERCENT_RE = re.compile(r"\d+(?:\.\d+)?\s*%")


def _section_by_key(sections: Sequence[Mapping[str, Any]], key: str) -> Mapping[str, Any]:
    title = STRL_TITLE_OF[key]
    return next((section for section in sections if section.get("title") == title),
                {"title": title, "body": "", "numbers": []})


def presence_findings(sections: Sequence[Mapping[str, Any]],
                      worksheet: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """View, forecast gap and valuation are present, and agree with the worksheet."""

    findings: list[dict[str, Any]] = []
    conclusion = str(_section_by_key(sections, "conclusion").get("body") or "")
    head = conclusion[:240]
    stance = _STANCE_RE.search(head)
    if stance is None:
        findings.append({"code": "view_missing", "section": STRL_TITLE_OF["conclusion"],
                         "detail": "结论第一段没有立场（看多/看空/中性/观察）"})
    elif worksheet and worksheet.get("stance") and stance.group(0) != worksheet["stance"]:
        findings.append({"code": "view_disagrees_with_worksheet",
                         "section": STRL_TITLE_OF["conclusion"], "detail": stance.group(0)})
    if not (_WATCH_GIVEN_RE.search(conclusion) or _WATCH_UNAVAILABLE_RE.search(conclusion)):
        findings.append({"code": "watch_price_missing", "section": STRL_TITLE_OF["conclusion"],
                         "detail": "写观察价，或「观察价：暂无法给出（原因）」"})
    forecast = _section_by_key(sections, "forecast_vs_consensus")
    body = str(forecast.get("body") or "")
    gap_only = bool(worksheet and worksheet.get("forecasts") and all(row.get("status") == "unavailable" for row in worksheet["forecasts"]))
    if gap_only:
        if not (re.search(r"consensus|一致预期", body, re.IGNORECASE) and re.search(r"无法|不可得|未取得|缺|不可比", body)):
            findings.append({"code": "forecast_unavailable_reason_missing", "section": forecast["title"]})
    elif not (re.search(r"consensus|一致预期", body, re.IGNORECASE) and _PERCENT_RE.search(body)
            and re.search(r"我们", body)):
        findings.append({"code": "forecast_gap_missing", "section": forecast["title"],
                         "detail": "需要我们 vs 一致预期的差异 %"})
    if not gap_only and not any(item.get("derivation") for item in forecast.get("numbers") or ()):
        findings.append({"code": "forecast_gap_without_expression", "section": forecast["title"]})
    valuation = _section_by_key(sections, "valuation")
    body = str(valuation.get("body") or "")
    unavailable = bool(_WATCH_UNAVAILABLE_RE.search(body))
    if unavailable:
        if not re.search(r"一致预期|consensus|卖方|券商|目标价", body, re.IGNORECASE):
            findings.append({"code": "valuation_unavailable_without_reference", "section": valuation["title"]})
    else:
        if not _WATCH_GIVEN_RE.search(body):
            findings.append({"code": "valuation_missing", "section": valuation["title"]})
        if not any(item.get("derivation") for item in valuation.get("numbers") or ()):
            findings.append({"code": "valuation_without_expression", "section": valuation["title"]})
    if worksheet:
        watch = worksheet.get("watch_price") or {}
        if watch.get("status") == "given" and watch.get("value") and not _digits_in(
                watch["value"], conclusion + body):
            findings.append({"code": "watch_price_disagrees_with_worksheet", "section": valuation["title"]})
    return findings


def appendix_findings(sections: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Each gap in the appendix names its kind; not_acquired names a request and 已尝试."""

    appendix = _section_by_key(sections, "unknowns")
    findings = []
    items = appendix.get("gap_items") or []
    if not items:
        findings.append({"code": "unknowns_without_gap_items", "section": appendix["title"]})
    body = str(appendix.get("body") or "")
    for item in items:
        if item.get("kind") == "not_acquired":
            ref = str(item.get("request_ref") or "")
            if ref and ref not in body:
                findings.append({"code": "appendix_request_not_cited", "section": appendix["title"],
                                 "detail": ref})
    if items and any(item.get("kind") == "not_acquired" for item in items) and not TRIED_RE.search(body):
        findings.append({"code": "appendix_without_tried", "section": appendix["title"]})
    return findings


def source_kinds_of(section: Mapping[str, Any]) -> set[str]:
    return {str(item.get("source_kind") or "") for item in section.get("source_refs") or ()}


def verify_document(
    sections: Sequence[Mapping[str, Any]], *, worksheet: Mapping[str, Any] | None,
    pack: Mapping[str, Any], live_claim_refs: set[str] | None = None,
    resolve_cell: Any = None, resolve_passage: Any = None,
) -> dict[str, Any]:
    """The deterministic verifier: citations, arithmetic, presence, lint.

    Contract change (batch D): the verifier no longer deletes sentences it
    could not source.  It checks every citation and every declared
    arithmetic, that the view, the forecast gap and the valuation are present,
    applies the reasoning lint, and either passes the whole document or
    rejects it with findings for one whole-document repair.
    """

    pack = _pack(pack)

    from .mission_deliverable import (
        MissionDeliverableError,
        pack_passage_resolver,
        validate_section,
    )
    from .product_substance import display_rounding_findings
    from .research_reasoning_lint import lint_text

    numbers_by_ref, passages_by_ref = pack_resolvers(pack)
    resolver = pack_passage_resolver(resolve_passage, passages_by_ref)
    findings: list[dict[str, Any]] = []
    for section in sections:
        title = str(section.get("title") or "")
        wire = {key: section[key] for key in ("title", "body", "claim_refs", "numbers", "gaps",
                                              "gap_items") if key in section}
        try:
            validate_section(wire, live_claim_refs=live_claim_refs, resolve_cell=resolve_cell,
                             resolve_passage=resolver, resolve_number=numbers_by_ref.get,
                             strict=True)
        except MissionDeliverableError as exc:
            findings.append({"code": "citation_or_contract", "section": title, "detail": str(exc)[:300]})
        findings.extend(lint_text(str(section.get("body") or ""), source_kinds=source_kinds_of(section),
                                  section=title))
        findings.extend({**item, "section": title}
                        for item in display_rounding_findings(str(section.get("body") or "")))
    if worksheet is not None:
        _, worksheet_findings = validate_worksheet(worksheet, pack)
        findings.extend({**item, "section": STRL_TITLE_OF["forecast_vs_consensus"]} for item in worksheet_findings)
        forecast = _section_by_key(sections, "forecast_vs_consensus")
        for row in worksheet.get("forecasts") or []:
            if row.get("metric") != "eps" or row.get("status") != "unavailable" or row.get("ours") is None or row.get("consensus") is None: continue
            for number in forecast.get("numbers") or []:
                ref = str(number.get("number_ref") or "")
                expression = str((number.get("derivation") or {}).get("expression") or "")
                if re.search(r"/eps_vs_consensus/(diff_pct|difference)$", ref) or (expression and _digits_in(row["ours"], expression) and _digits_in(row["consensus"], expression)):
                    findings.append({"code": "forecast_eps_basis_spread_in_document", "section": forecast["title"], "detail": row["year"] + ": incomparable EPS must not produce a numeric spread"})
    findings.extend(presence_findings(sections, worksheet))
    findings.extend(appendix_findings(sections))
    whole = "\n".join(str(section.get("body") or "") for section in sections)
    if material_missing_count(whole) > 2:
        findings.append({"code": "material_missing_over_limit",
                         "detail": f"「材料缺少」出现 {material_missing_count(whole)} 次，上限 2"})
    return {"verdict": "pass" if not findings else "reject", "findings": findings}


def pack_resolvers(pack: Mapping[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    """``(number_ref -> text, passage_ref -> text)`` from the pack's own tags."""

    pack = _pack(pack)

    numbers: dict[str, str] = {}
    passages: dict[str, str] = {}
    for tag in (pack.get("tags") or {}):
        entry = tag_entry(pack, str(tag))
        if entry is None or not entry.get("text"):
            continue
        if entry["kind"] == "number":
            numbers[entry["ref"]] = str(entry["text"])
        elif entry["kind"] == "passage":
            passages[entry["ref"]] = str(entry["text"])
    return numbers, passages


STRL_VERIFIER_FINDING_CODES: tuple[str, ...] = (
    "unsupported_figure", "arithmetic_error", "view_missing", "forecast_gap_missing",
    "valuation_missing", "reasoning_lint", "material_missing_unbound", "contradiction",
)


def build_verifier_prompt(sections: Sequence[Mapping[str, Any]], *, worksheet: Mapping[str, Any],
                          pack: Mapping[str, Any]) -> str:
    """The model verifier: what the deterministic checks cannot read.

    It checks; it does not rewrite and it does not delete.  The closed output
    is a verdict and findings with the codes above.
    """

    pack = _pack(pack)

    from .research_reasoning_lint import VERIFIER_LINT_INSTRUCTIONS

    return "\n".join([
        "Independently verify this Initial Screen against the cited material. Do not rewrite it.",
        "Check: every figure is verbatim in its cited tag, or a declared arithmetic over cited",
        "figures, or labelled 我们的估计 and bound to our model; the arithmetic is right; the view",
        "A validated number.display is also allowed: its exact source_value is retained with",
        "source_text_hash and same-unit one- or two-decimal ROUND_HALF_UP; display_value may be an arithmetic operand.",
        "EPS is comparable only under explicit equal held definitions: GAAP with GAAP; adjusted/core with the same definition_ref. Unknown basis or GAAP versus adjusted requires an unavailable comparison retaining known sides. Reject cross-basis EPS spreads, attribution of their difference to business drivers, or comparison to adjusted guidance without a proved common definition.",
        "(stance + watch price or an explicit reason), our-vs-consensus forecast gap with its",
        "assumption, and the valuation (multiple x EPS = watch price, current price, implied",
        "return, multiple rationale) are present and agree with the worksheet; every 「材料缺少」",
        "is bound to a MaterialRequest; and the reasoning rules below hold:",
        *[f"- {rule}" for rule in VERIFIER_LINT_INSTRUCTIONS],
        "Return raw JSON only: {\"verdict\": \"pass|reject\", \"findings\": [{\"code\": \""
        + "|".join(STRL_VERIFIER_FINDING_CODES) + "\", \"section\": \"…\", \"detail\": \"one sentence\"}]}",
        "A pass has no findings; a reject has at least one.",
        "Worksheet:", json.dumps(worksheet, ensure_ascii=False, sort_keys=True),
        "Document:", json.dumps([{k: s.get(k) for k in ("title", "body", "numbers", "gap_items")}
                                 for s in sections], ensure_ascii=False),
        "Material pack:", str(pack.get("text") or ""),
    ])


def parse_verifier_output(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"verdict", "findings"} \
            or value["verdict"] not in ("pass", "reject") or not isinstance(value["findings"], list):
        raise InitialScreenContractError("verifier output has an invalid closed shape")
    findings = []
    for item in value["findings"]:
        if (not isinstance(item, Mapping) or set(item) != {"code", "section", "detail"}
                or item.get("code") not in STRL_VERIFIER_FINDING_CODES
                or not isinstance(item.get("section"), str)
                or not isinstance(item.get("detail"), str) or not item["detail"].strip()):
            raise InitialScreenContractError("verifier finding has an invalid closed shape or code")
        findings.append({"code": f"verifier:{item['code']}", "section": str(item.get("section") or "")[:60],
                         "detail": str(item.get("detail") or "")[:300]})
    if (value["verdict"] == "pass") != (not findings):
        raise InitialScreenContractError("verifier verdict and findings disagree")
    return {"verdict": value["verdict"], "findings": findings}


def strl_contract_record(*, pack: Mapping[str, Any], worksheet: Mapping[str, Any],
                         verdict: Mapping[str, Any]) -> dict[str, Any]:
    """What the published version carries about how it was made.

    The stance and the watch price are contract fields (owner decision 1:
    观察价作为合约字段留痕), and the pack is bound by hash and recipe.
    """

    pack = _pack(pack)

    from .research_reasoning_lint import lint_fingerprint

    watch = worksheet.get("watch_price") or {}
    inputs = pack.get("inputs") or []
    return {
        "version": STRL_CONTRACT_VERSION,
        "product_contract": product_contract_fingerprint(),
        "lint": lint_fingerprint(),
        "pack": {"content_hash": str(pack.get("content_hash") or ""),
                 "recipe_version": str(pack.get("recipe_version") or ""),
                 "as_of": str(pack.get("as_of") or ""),
                 "total_chars": len(str(pack.get("text") or "")),
                 # The pack's own inputs (with doc_kind and date): rubric H
                 # finds the latest transcript and release among them.
                 "inputs": json.loads(json.dumps(inputs, default=str))[:200]
                 if isinstance(inputs, list) else [],
                 "gap_request_refs": list((pack.get("sufficiency") or {}).get("gap_request_refs") or [])},
        "stance": worksheet.get("stance"),
        "watch_price": {key: watch.get(key) for key in (
            "status", "value", "currency", "multiple", "multiple_basis", "eps", "expression",
            "current_price", "implied_return_pct", "reason") if watch.get(key) is not None},
        "worksheet": json.loads(json.dumps(dict(worksheet), ensure_ascii=False)),
        "verifier": {"verdict": verdict.get("verdict"),
                     "finding_codes": sorted({str(item.get("code")) for item in verdict.get("findings") or ()})},
    }


def assess_strl_gate(
    *, playbook: Mapping[str, Any], checklist_entry: Mapping[str, Any],
    sections: Sequence[Mapping[str, Any]], verdict: Mapping[str, Any],
    delta_vs_prior: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The Playbook's four questions over the STRL-shaped document.

    A document with any failed section (empty, heading-only, gap-only) cannot
    pass, whatever else it says (ap 1).
    """

    from .product_substance import failed_sections

    stage = next((item for item in playbook.get("stages", ())
                  if item.get("stage_ref") == "initial_screen"), None)
    questions = list((stage or {}).get("exit_gate", {}).get("questions") or [])
    missing_items = [item["label"] for item in checklist_entry.get("items", ())
                     if item.get("status") != "complete"]
    failed = failed_sections(sections)
    failed_titles = {item["title"] for item in failed}

    def ok(*keys: str) -> bool:
        return not any(STRL_TITLE_OF[key] in failed_titles for key in keys)

    structural = [
        {"check": "source_base", "answer": not missing_items,
         "basis": "资料底座四项全部齐备" if not missing_items else "还缺：" + "、".join(missing_items)},
        {"check": "number_provenance", "answer": verdict.get("verdict") == "pass",
         "basis": ("核验通过：每个数字逐字来自所引段落/数字、声明算式或绑定模型的我们的估计"
                   if verdict.get("verdict") == "pass" else "核验未通过")},
        {"check": "key_driver", "answer": ok("conclusion", "business_model", "forecast_vs_consensus"),
         "basis": ("结论、商业模式与盈利预测三节都有实质内容"
                   if ok("conclusion", "business_model", "forecast_vs_consensus")
                   else "结论/商业模式/盈利预测有空节")},
        {"check": "street_and_risk", "answer": ok("forecast_vs_consensus", "valuation", "risks", "trading"),
         "basis": ("与一致预期的差异、估值、风险与交易角度都已写出"
                   if ok("forecast_vs_consensus", "valuation", "risks", "trading")
                   else "预测差/估值/风险/交易角度里还有空节")},
    ]
    answers = [
        {**item, "question": questions[index] if index < len(questions) else item["check"],
         "status": "passed" if item["answer"] else "failed"}
        for index, item in enumerate(structural)
    ]
    passed = all(item["answer"] for item in answers) and not failed
    return {
        "schema_version": SCHEMA_VERSION, "passed": passed, "answers": answers,
        "contract": STRL_CONTRACT_VERSION,
        "failed_sections": failed,
        "delta_vs_prior": None if delta_vs_prior is None
                          else json.loads(json.dumps(dict(delta_vs_prior), ensure_ascii=False)),
        "rationale": ("四问全部为是，九节都有实质内容，核验通过" if passed
                      else "；".join([item["basis"] for item in answers if not item["answer"]]
                                     + [f"{item['title']}：{item['reason']}" for item in failed])),
    }


__all__ = [
    "STRL_CONTRACT_VERSION",
    "STRL_KEYS",
    "STRL_RULES",
    "STRL_SECTIONS",
    "STRL_TEMPLATE_REF",
    "STRL_TITLES",
    "InitialScreenContractError",
    "appendix_findings",
    "assess_strl_gate",
    "build_document_prompt",
    "build_verifier_prompt",
    "build_worksheet_prompt",
    "pack_resolvers",
    "parse_document_output",
    "parse_verifier_output",
    "presence_findings",
    "strl_contract_record",
    "validate_worksheet",
    "verify_document",
    "KIND",
    "raw_section_body",
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
