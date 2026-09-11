"""P15a: the answer -- a closed shape, checked before the owner sees it.

Ask v1 asked for four fields and hoped.  ``answer`` was prose with tags in it,
``gaps`` was a list of sentences, ``confidence`` was whatever came back, and
anything the model invented arrived unchallenged: an unshown tag was silently
dropped from the citation list while the sentence that leaned on it stayed in
the answer, and "we do not know" was indistinguishable from "we have not
looked".

Four things change here, and each is a rule the roadmap's acceptance test
("PM 用自然语言问 10 个真实问题，≥8 个被评为可用") turns on.

**Sentences carry their own refs.**  P13ap's lesson from the live Initial
Screens is that prose with citation tags written into it cannot survive their
removal -- the published screens carry sentences whose subject left with the
tag.  So the model returns ``[{text, refs}]`` rows, the tags never enter the
text, and the body the owner reads is assembled here.  This is also what makes
"cited only what was shown" checkable per sentence rather than per answer.

**An unknown is typed.**  "缺数据" is not an unknown; "缺 ACN 最近四个季度的
bookings 金额，那是 ``sell_side_report`` 或 ``transcript``，去 alphaengine 取"
is.  Each unknown names a content kind from ``SourceCapabilityMap``'s closed
list and a source that actually yields it, and the check rejects a pairing the
map says is impossible.  That is the difference between an answer that says it
is stuck and an answer that says what would unstick it -- and it is what the
refresh in :mod:`ask_refresh` reads to decide where to look.

**A view question compares our understanding with the observed market view.**
The owner's later clarification allows earnings delivery, expectation revision
or valuation change to support an investment, separately or together. Agreement
does not itself make a view worthless and disagreement must not be invented.
A question of
kind ``view`` or ``debate`` must come back with the five slots P12a already
froze for the dossier's variant view -- our view, the market's, where it is
wrong, the pathway, the observable signals -- or the answer is marked as
missing them.  One vocabulary for the whole system, not a second one here.

**Confidence stays three words.**  ``high``/``medium``/``low`` and nothing
else, because Q1's ``confidence_stated`` check is written against exactly those
three and a fourth word would be an answer that fails a deterministic check for
being more honest.  A refusal is therefore not a confidence: it is
``refused: true`` with a reason from a closed list, alongside a ``low``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from .ask_context import VIEW_KINDS, render_context
from .company_dossier import VARIANT_SLOTS
from .source_capability_map import (
    CAPABILITIES,
    CONTENT_KINDS,
    connection_status,
    sources_for,
)

SCHEMA_VERSION = "0.1"

# Q1's ``confidence_stated`` check is written against these three words.
CONFIDENCE: tuple[str, ...] = ("high", "medium", "low")

# Why an answer refuses to answer.  Closed, because the page shows a different
# sentence for each and a fifth word would render as itself.
REFUSAL_REASONS: tuple[str, ...] = (
    # nothing was shown that bears on the question at all
    "no_evidence_shown",
    # the question is about something outside the coverage list
    "outside_coverage",
    # the material exists somewhere and is not on this Core
    "needs_refresh",
    # the question could not be read as a question about these companies
    "question_not_understood",
)

# What a refusal reason says to the owner, in their language.
REFUSAL_LABELS: Mapping[str, str] = {
    "no_evidence_shown": "账本里没有任何与这个问题相关的材料",
    "outside_coverage": "这个问题问的对象不在覆盖名单里",
    "needs_refresh": "回答它需要的材料不在这个账本上，要先去取",
    "question_not_understood": "这个问题没能被读成一个关于这些公司的问题",
}

# What the page and the checks call each variant-view slot.  P12a's five words
# in the owner's language, in one place.
SLOT_LABELS: Mapping[str, str] = {
    "our_view": "我们的看法", "market_view": "市场的看法",
    "where_market_is_wrong": "市场错在哪",
    "convergence_pathway": "市场向我们靠拢的路径",
    "observable_signals": "可观察的信号",
    "market_view_reason": "为什么没有市场的看法",
}
# The sentence an answer writes when it has no material on where the market
# stands.  First entry is the one this module normalises to.
NO_MARKET_VIEW: tuple[str, ...] = ("未知（没有一致预期、评级、sales note 或大众叙事的材料）",
                                   "未知", "不知道", "无", "unknown", "n/a")

MAX_SENTENCES = 24
MAX_SENTENCE_CHARS = 600
MAX_UNKNOWNS = 6
MAX_REFS_PER_SENTENCE = 8
MAX_QUERY_CHARS = 200

# The checks this module runs on every answer, in order.  Separate from Q1's
# rubric: that one grades an artefact after the fact, this one decides what the
# owner is shown.  They overlap on purpose -- ``cites_only_shown_rows`` here is
# ``cites_only_shown_claims`` there -- because a check that only runs in the
# grader is a check the owner still had to read past.
VERIFICATION_CHECKS: tuple[str, ...] = (
    "cites_only_shown_rows",
    "numeric_sentences_cite",
    "confidence_in_vocabulary",
    "unknowns_typed",
    "market_vs_us_present",
    "refresh_names_a_capable_source",
    "refusal_states_why",
)

CHECK_LABELS: Mapping[str, str] = {
    "cites_only_shown_rows": "只引用了这次展示过的材料",
    "numeric_sentences_cite": "每一句带数字的话都有出处",
    "confidence_in_vocabulary": "信心是三个词之一",
    "unknowns_typed": "每个「不知道」都点名了缺什么内容、去哪取",
    "market_vs_us_present": "看法类问题写了我们与市场的差别和收敛路径",
    "refresh_names_a_capable_source": "建议的补搜指向一个真能给出那类内容的来源",
    "refusal_states_why": "拒答说明了理由",
}


class AskAnswerError(RuntimeError):
    """The answer came back in a shape this module will not pass on."""


def _text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _result(check: str, *, status: str, findings: Sequence[Mapping[str, Any]] = (),
            detail: str = "") -> dict[str, Any]:
    return {
        "check": check, "label": CHECK_LABELS[check], "status": status,
        "count": len(findings), "findings": [dict(f) for f in findings][:20],
        "detail": detail,
    }


# ---------------------------------------------------------------------------
# the prompt
# ---------------------------------------------------------------------------

def capability_lines(mission: Mapping[str, Any] | None) -> list[str]:
    """One line per content kind: what it is called and who can give it.

    Rendered from ``SourceCapabilityMap`` rather than written out, so that a
    connector arriving on the S line becomes an answer this panel can suggest
    without anybody remembering to edit a prompt.  Connected sources come
    first, and a source the mission has not connected is marked as such:
    suggesting a refresh from something nobody plugged in is a suggestion the
    owner cannot act on.
    """

    status = connection_status(mission)
    lines = []
    for kind in CONTENT_KINDS:
        slugs = sources_for(kind)
        if not slugs:
            continue
        rendered = []
        for slug in slugs:
            state = status.get(slug, "undeclared")
            mark = "" if state == "connected" else f"[{state}]"
            rendered.append(f"{slug}{mark}")
        lines.append(f"- {kind}: " + "、".join(rendered))
    return lines


def build_prompt(context: Mapping[str, Any], *, mission: Mapping[str, Any] | None = None) -> str:
    """Everything shown, the shape required, and the two refusals allowed."""

    wants_variant = bool(context.get("wants_market_vs_us"))
    shape = [
        '{"sentences": [{"text": "<一句话，句中不要写标签>", "refs": ["C7", "D2"]}],',
        ' "confidence": "high|medium|low",',
        ' "refused": false, "refusal_reason": null, "refusal_detail": null,',
        ' "unknowns": [{"what": "<缺的是什么>", "content_kind": "<下表中的一个词>",'
        ' "source": "<下表中的一个来源>"}],',
        (' "market_vs_us": {' + ", ".join(f'"{slot}": "..."' for slot in VARIANT_SLOTS)
         + ', "market_view_available": true, "market_view_reason": null,'
         ' "refs": ["C7"]},'),
        ' "refresh_suggested": {"content_kind": "...", "source": "...", "query": "..."}}',
    ]
    lines = [
        "你是一套股票研究系统的分析师。只用下面展示给你的材料回答 owner 的问题。",
        "每一条材料都有一个标签（C=账本结论，D=公司档案，B=还在争的问题，F=我们的模型，",
        "V=估值，P=股价，S=一致预期，K=日程，E=最近事件，G=我们的判断，R=反思，",
        "T=已立论点，N=你上次的反馈）。你引用的每一个标签都必须来自这次展示的材料，",
        "编造的标签会被丢掉，靠它的那句话也会被丢掉。",
        "标签只写在 refs 里，绝不要写进句子正文——正文要在标签被去掉之后仍然读得通。",
        "数字只能逐字来自被引材料；没有数字可用就说没有，不要估一个。",
        "答不了就直说不知道：证据不足时「我不知道，缺的是 X，要去 Y 取」是满分答案。",
        "用 owner 的语言回答（问题是中文就用中文）。",
        "",
        "只返回一个 JSON 对象，不要 markdown 代码块，形状如下：",
        *shape,
        "",
        "unknowns 里的 content_kind 与 source 必须来自这张表（方括号是这套系统与它的连接状态）：",
        *capability_lines(mission),
        "",
        "refresh_suggested：当且仅当账本上的材料不足、而上面某个来源能补上时给出；否则填 null。",
        "refused：当展示的材料完全不能支撑任何回答时填 true，并从 "
        + "、".join(REFUSAL_REASONS) + " 里选一个 refusal_reason，refusal_detail 写一句人话。",
    ]
    if wants_variant:
        lines += [
            "",
            "这是一个「看法」类问题。先说清对行业、业务本质、竞争优势和关键经营驱动的判断，"
            "再说明这些认知对当前问题的意义；不强行制造与市场的分歧。",
            "目标是理解市场现在怎么想，并判断下一步会怎样，而不是只描述我们是否与市场一致。"
            "先明确当前主判断，承担这个判断；再说明另一情景在什么条件下成立、各自会造成什么影响。"
            "不要用『A 有可能，B 也有可能』代替结论。信息不足时指出决定分歧的具体缺口，"
            "并在证据允许的范围内给出当前倾向，不能为了语气坚决而编造事实。",
            "涉及投资建议时，基金持有期不超过十二个月。区分盈利增长兑现、盈利预期变化和估值变化，"
            "其中单一来源也可有价值；不要求一定共振或必须有短期催化剂。"
            "只引用已展示的同口径模型、价格和预期；缺少回报桥时不自行编算收益。",
            "market_vs_us 的五格必须**每一格都写满**，空一格就算没写："
            + "、".join(f"{slot}（{SLOT_LABELS[slot]}）" for slot in VARIANT_SLOTS)
            + "。其中 market_view 只能来自被展示的一致预期、评级、sales note 或大众叙事；",
            "没有这些材料时把 market_view_available 设为 false，并在 market_view_reason 里"
            "写清是缺什么材料——不要替市场编一个看法，也不要只写一个「未知」了事。",
            "where_market_is_wrong 写有证据的假设差异；若尚无证据证明市场错了，明确写没有已证实的分歧。"
            "convergence_pathway 可以说明盈利兑现或估值正常化的验证路径，不必虚构市场改口。",
            "把卖方一致预期、sales desk 观察和社媒注意力分开；它们不等于买方共识或仓位。"
            "区分事实、研究推断和未知，说明反证与下一验证信号；资料更新时重新评估旧观点，"
            "不把一次股价变化直接当成因果或论点被证实。",
            "observable_signals 必须对应主情景和替代情景的条件：观察什么指标或事件、"
            "来自哪里、什么变化会支持或推翻当前判断、何时复查。"
            "这是待纳入 tracking 的观测建议；没有实际调度凭证时不要声称已经安排跟踪。",
        ]
    else:
        lines += ["", "这不是看法类问题，market_vs_us 填 null。"]
    lines += ["", "=" * 60, "", render_context(context), "", "=" * 60, "",
              f"问题：{context['question']}"]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# parsing and verification
# ---------------------------------------------------------------------------

def parse_answer(payload: Any, *, context: Mapping[str, Any]) -> dict[str, Any]:
    """The model's reply, narrowed to the closed shape and checked.

    Never raises on a bad reply: a model that answers badly is a thing the
    owner has to be able to see, and an exception here would show them a
    stack-shaped error instead of an answer with its faults named.  The one
    exception is a reply that is not an object at all, which is not an answer.
    """

    if not isinstance(payload, Mapping):
        raise AskAnswerError("模型没有返回一个可用的答案对象")
    shown = {row["tag"]: row for row in context.get("shown") or ()}
    findings: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []

    sentences, unshown = _sentences(payload.get("sentences"), payload.get("answer"), shown)
    checks.append(_result(
        "cites_only_shown_rows",
        status="fail" if unshown else "pass",
        findings=[{"code": "unshown_tag", "tag": tag} for tag in unshown],
        detail=(f"引用全部来自这次展示的 {len(shown)} 条材料" if not unshown
                else f"{len(unshown)} 个标签不在展示范围内，已从引用里去掉"),
    ))

    numeric = [row for row in sentences
               if re.search(r"\d", row["text"]) and not row["refs"]]
    checks.append(_result(
        "numeric_sentences_cite",
        status="fail" if numeric else "pass",
        findings=[{"code": "number_without_ref", "text": row["text"][:120]}
                  for row in numeric],
        detail=("每一句带数字的话都有出处" if not numeric
                else f"{len(numeric)} 句带数字的话没有引用"),
    ))

    confidence = payload.get("confidence")
    valid_confidence = confidence in CONFIDENCE
    checks.append(_result(
        "confidence_in_vocabulary",
        status="pass" if valid_confidence else "fail",
        findings=([] if valid_confidence
                  else [{"code": "confidence_not_in_vocabulary", "value": str(confidence)[:40]}]),
        detail=(f"信心：{confidence}" if valid_confidence
                else "没有给出 high / medium / low 之一，按 low 记"),
    ))
    if not valid_confidence:
        confidence = "low"

    unknowns, bad_unknowns = _unknowns(payload.get("unknowns"))
    checks.append(_result(
        "unknowns_typed",
        status="fail" if bad_unknowns else "pass",
        findings=bad_unknowns,
        detail=(f"{len(unknowns)} 个缺口都点名了内容类型与来源" if not bad_unknowns
                else f"{len(bad_unknowns)} 个缺口没有说清缺什么或去哪取"),
    ))

    refused = bool(payload.get("refused"))
    reason = payload.get("refusal_reason")
    if refused and reason not in REFUSAL_REASONS:
        reason = None
    detail = _text(payload.get("refusal_detail"), 600) or None
    checks.append(_result(
        "refusal_states_why",
        status="fail" if (refused and (reason is None or detail is None)) else "pass",
        findings=([] if not refused or (reason and detail)
                  else [{"code": "refusal_without_reason",
                         "reason": str(payload.get("refusal_reason"))[:40]}]),
        detail=("没有拒答" if not refused
                else (f"拒答理由：{REFUSAL_LABELS.get(reason, reason)}"
                      if reason else "拒答但没有给出封闭词表里的理由")),
    ))

    wants_variant = bool(context.get("wants_market_vs_us"))
    variant, variant_unshown, empty_slots = _market_vs_us(
        payload.get("market_vs_us"), shown)
    unshown = sorted(set(unshown) | set(variant_unshown))
    # A variant view with a blank slot is not a variant view. The five slots
    # are the owner's question -- where do we differ, why is the market wrong,
    # what brings it round, what will we see -- and an answer that leaves the
    # pathway empty has answered "we disagree" and stopped.
    variant_findings: list[dict[str, Any]] = []
    if wants_variant and not refused:
        if variant is None:
            variant_findings.append({"code": "market_vs_us_missing"})
        else:
            variant_findings += [{"code": "market_vs_us_slot_empty", "slot": slot}
                                 for slot in empty_slots]
    checks.append(_result(
        "market_vs_us_present",
        status="fail" if variant_findings else ("pass" if wants_variant else "skipped"),
        findings=variant_findings,
        detail=("这不是看法类问题" if not wants_variant
                else ("写了我们与市场的差别与收敛路径" if not variant_findings
                      else ("看法类问题没有写我们与市场的差别" if variant is None
                            else "这几格是空的：" + "、".join(
                                SLOT_LABELS.get(slot, slot) for slot in empty_slots)))),
    ))

    refresh, refresh_findings = _refresh(payload.get("refresh_suggested"))
    checks.append(_result(
        "refresh_names_a_capable_source",
        status="fail" if refresh_findings else ("pass" if refresh else "skipped"),
        findings=refresh_findings,
        detail=("没有建议补搜" if refresh is None and not refresh_findings
                else (f"建议去 {refresh['source']} 取 {refresh['content_kind']}"
                      if refresh else "建议的补搜指向一个给不出那类内容的来源")),
    ))

    body = assemble_body(sentences, variant)
    citations = []
    seen: set[str] = set()
    for row in sentences:
        for tag in row["refs"]:
            if tag in seen:
                continue
            seen.add(tag)
            citations.append(dict(shown[tag]))
    for tag in (variant or {}).get("refs", ()):
        if tag not in seen:
            seen.add(tag)
            citations.append(dict(shown[tag]))
    findings = [f for check in checks for f in check["findings"]]
    return {
        "schema_version": SCHEMA_VERSION,
        "answer": body,
        "sentences": sentences,
        "citations": citations,
        "cited_tags": [row["tag"] for row in citations],
        "confidence": confidence,
        "refused": refused,
        "refusal_reason": reason,
        "refusal_label": REFUSAL_LABELS.get(reason) if reason else None,
        "refusal_detail": detail,
        "unknowns": unknowns,
        # Q1's rubric and the cockpit page both read ``gaps``; it is the
        # unknowns rendered as sentences rather than a second list that could
        # disagree with them.
        "gaps": [f"{item['what']}（{item['content_kind']} → {item['source']}）"
                 for item in unknowns],
        "market_vs_us": variant,
        "refresh_suggested": refresh,
        "verification": {
            "schema_version": SCHEMA_VERSION,
            "checks": checks,
            "failed_checks": [c["check"] for c in checks if c["status"] == "fail"],
            "passed": not any(c["status"] == "fail" for c in checks),
            "findings": findings,
        },
    }


def assemble_body(
    sentences: Sequence[Mapping[str, Any]], variant: Mapping[str, Any] | None,
) -> str:
    """The prose the owner reads, built from the rows rather than by the model.

    The tags were never in it, so nothing is left behind when they are gone --
    the whole point of asking for sentences instead of a paragraph.
    """

    body = " ".join(row["text"] for row in sentences).strip()
    if variant is None:
        return body
    parts = [body] if body else []
    for slot in VARIANT_SLOTS:
        value = variant.get(slot)
        if not value:
            continue
        line = f"{SLOT_LABELS[slot]}：{value}"
        if slot == "market_view" and not variant.get("market_view_available"):
            reason = variant.get("market_view_reason")
            line += f"（{reason}）" if reason else ""
        parts.append(line)
    return "\n".join(parts)


def _sentences(
    value: Any, fallback: Any, shown: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Rows of ``{text, refs}``, with unshown tags removed and reported.

    A model that returns a plain ``answer`` string instead of rows is not
    refused: the string becomes one uncited sentence, which the numeric check
    will then fail if it carries figures.  Refusing outright would turn a
    formatting slip into an outage of the question panel.
    """

    rows: list[dict[str, Any]] = []
    unshown: list[str] = []
    items = value if isinstance(value, list) else []
    if not items and isinstance(fallback, str) and fallback.strip():
        items = [{"text": fallback, "refs": []}]
    for item in items[:MAX_SENTENCES]:
        if isinstance(item, str):
            item = {"text": item, "refs": []}
        if not isinstance(item, Mapping):
            continue
        text = _text(item.get("text"), MAX_SENTENCE_CHARS)
        if not text:
            continue
        refs: list[str] = []
        raw = item.get("refs") if isinstance(item.get("refs"), list) else []
        for tag in raw[:MAX_REFS_PER_SENTENCE]:
            tag = str(tag).strip()
            if tag in shown:
                if tag not in refs:
                    refs.append(tag)
            elif tag:
                unshown.append(tag)
        rows.append({"text": text, "refs": refs})
    return rows, sorted(set(unshown))


def _unknowns(value: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Each unknown typed by content kind and by a source that yields it."""

    kept: list[dict[str, Any]] = []
    bad: list[dict[str, Any]] = []
    for item in (value if isinstance(value, list) else [])[:MAX_UNKNOWNS]:
        if isinstance(item, str):
            bad.append({"code": "unknown_without_a_type", "what": _text(item, 120)})
            continue
        if not isinstance(item, Mapping):
            continue
        what = _text(item.get("what"), 300)
        kind = str(item.get("content_kind") or "").strip()
        source = str(item.get("source") or "").strip()
        if not what:
            continue
        if kind not in CONTENT_KINDS:
            bad.append({"code": "unknown_content_kind", "what": what[:120],
                        "content_kind": kind[:40]})
            continue
        capable = sources_for(kind)
        if source not in CAPABILITIES:
            bad.append({"code": "unknown_source", "what": what[:120], "source": source[:40]})
            continue
        if source not in capable:
            bad.append({"code": "source_cannot_answer_content_kind", "what": what[:120],
                        "content_kind": kind, "source": source})
            continue
        kept.append({"what": what, "content_kind": kind, "source": source,
                     "alternatives": [slug for slug in capable if slug != source][:3]})
    return kept, bad


def _market_vs_us(
    value: Any, shown: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, Any] | None, list[str], list[str]]:
    """The variant view, the tags it invented, and the slots it left empty.

    ``market_view_available`` is P12a's own typed shape, carried here for the
    reason P12a carries it: "we do not know where the market is" and "the
    market agrees with us" have to look different, and an answer that writes
    「未知」 into ``market_view`` without saying that is what it means is
    indistinguishable from one that invented a market view.  So when the flag
    is false the slot is normalised to the honest sentence and a reason is
    required; when it is true the slot has to say something.
    """

    if not isinstance(value, Mapping):
        return None, [], list(VARIANT_SLOTS)
    slots = {slot: _text(value.get(slot), MAX_SENTENCE_CHARS) for slot in VARIANT_SLOTS}
    available = value.get("market_view_available")
    if not isinstance(available, bool):
        # Not stated: infer it from whether a market view was written, and
        # record the inference rather than pretending it was declared.
        available = bool(slots["market_view"]) and slots["market_view"] not in NO_MARKET_VIEW
    reason = _text(value.get("market_view_reason"), 400) or None
    if not available:
        slots["market_view"] = NO_MARKET_VIEW[0]
    empty = [slot for slot in VARIANT_SLOTS if not slots[slot]]
    if not available and reason is None:
        empty.append("market_view_reason")
    refs: list[str] = []
    unshown: list[str] = []
    raw = value.get("refs") if isinstance(value.get("refs"), list) else []
    for tag in raw[:MAX_REFS_PER_SENTENCE]:
        tag = str(tag).strip()
        if tag in shown:
            if tag not in refs:
                refs.append(tag)
        elif tag:
            unshown.append(tag)
    block = {**slots, "market_view_available": available,
             "market_view_reason": reason, "refs": refs}
    return block, unshown, empty


def _refresh(value: Any) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    if not isinstance(value, Mapping):
        return None, []
    kind = str(value.get("content_kind") or "").strip()
    source = str(value.get("source") or "").strip()
    query = _text(value.get("query"), MAX_QUERY_CHARS)
    if not (kind or source or query):
        return None, []
    if kind not in CONTENT_KINDS:
        return None, [{"code": "unknown_content_kind", "content_kind": kind[:40]}]
    if source not in CAPABILITIES:
        return None, [{"code": "unknown_source", "source": source[:40]}]
    if source not in sources_for(kind):
        return None, [{"code": "source_cannot_answer_content_kind",
                       "content_kind": kind, "source": source}]
    if not query:
        return None, [{"code": "refresh_without_a_query"}]
    return {"content_kind": kind, "source": source, "query": query}, []


__all__ = [
    "AskAnswerError",
    "CHECK_LABELS",
    "CONFIDENCE",
    "MAX_UNKNOWNS",
    "REFUSAL_LABELS",
    "NO_MARKET_VIEW",
    "REFUSAL_REASONS",
    "SLOT_LABELS",
    "SCHEMA_VERSION",
    "VERIFICATION_CHECKS",
    "VIEW_KINDS",
    "assemble_body",
    "build_prompt",
    "capability_lines",
    "parse_answer",
]
