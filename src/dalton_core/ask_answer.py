"""Batch G: the answer an analyst gives -- four parts, checked before the owner sees it.

The owner's ask (2026-10-09): 「看怎么才能像个真正的分析师那样，在读了这么多报告之后形成有价值的回答」.
An analyst asked why a stock moved answers in a fixed order, and this module
holds the model to it:

1. **发生了什么** -- from first-hand material (the company's own filing and call,
   the tape), each sentence citing what it rests on (``[P…]`` passages,
   ``[M…]`` the computed move, ``[E…]`` events, ``[N…]`` the numbers pack).
2. **市场原本预期什么** -- the consensus and the previews: an ``N`` consensus row
   for 「一致预期」, a broker or sales note by name otherwise.
3. **我们的看法与是否改变** -- the current house view (``O`` rows) and whether
   this material strengthens, weakens or leaves it, written as a view.  Our
   own NO_CHANGE judgement is a starting point here, never evidence anywhere.
4. **不确定点** -- what could still change the answer; the cockpit appends
   「已尝试：…」 (the searches that ran) and 「正在获取：…」 (the MaterialRequest it
   opened) deterministically, the model never writes those.

Every sentence is ``{text, refs, basis, derivation}``.  Numbers follow the
deliverable rules: a figure appears in the material its sentence cites
(verbatim, or rounded / rescaled from it), or the sentence declares a
``derivation`` that re-computes from cited figures, or it is labelled
「我们的估计」 (``basis: our_estimate``).

The deterministic verifier (:func:`parse_answer`) checks: citations are
among the rows shown and (for passages) still resolve to their hashed text;
numbers are sourced; the four parts are present; what happened cites
evidence and not our own view; and a reasoning lint -- D's
``research_reasoning_lint`` when it is on this Core, otherwise a minimal local
set (one broker is not a consensus; a price attribution needs a benchmark).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Callable

from .ask_context import VIEW_KINDS, render_context
from .company_dossier import VARIANT_SLOTS
from .final_text_contract import final_text_instructions
from .source_capability_map import CAPABILITIES, CONTENT_KINDS, connection_status, sources_for

SCHEMA_VERSION = "0.2"

CONFIDENCE: tuple[str, ...] = ("high", "medium", "low")

REFUSAL_REASONS: tuple[str, ...] = (
    "no_evidence_shown", "outside_coverage", "needs_refresh", "question_not_understood",
)
REFUSAL_LABELS: Mapping[str, str] = {
    "no_evidence_shown": "账本里没有任何与这个问题相关的材料",
    "outside_coverage": "这个问题问的对象不在覆盖名单里",
    "needs_refresh": "回答它需要的材料不在这个账本上，要先去取",
    "question_not_understood": "这个问题没能被读成一个关于这些公司的问题",
}

#: The four parts, in reading order.
PARTS: tuple[str, ...] = ("what_happened", "market_expected", "our_view", "uncertainties")
PART_TITLES: Mapping[str, str] = {
    "what_happened": "发生了什么",
    "market_expected": "市场原本预期什么",
    "our_view": "我们的看法",
    "uncertainties": "不确定点",
}
#: Whether this material changes the house view.  Closed: the page prints one
#: sentence per word.
VIEW_CHANGES: tuple[str, ...] = ("strengthens", "weakens", "no_change", "unclear")
VIEW_CHANGE_LABELS: Mapping[str, str] = {
    "strengthens": "这次的材料加强了这个看法",
    "weakens": "这次的材料削弱了这个看法",
    "no_change": "这次的材料没有改变这个看法",
    "unclear": "这次的材料还不足以判断是否改变",
}
NUMBER_BASES: tuple[str, ...] = ("verbatim", "derivation", "our_estimate")
OUR_ESTIMATE_WORDS = "我们的估计"

SLOT_LABELS: Mapping[str, str] = {
    "our_view": "我们的看法", "market_view": "市场的看法",
    "where_market_is_wrong": "市场错在哪",
    "convergence_pathway": "市场向我们靠拢的路径",
    "observable_signals": "可观察的信号",
    "market_view_reason": "为什么没有市场的看法",
}
NO_MARKET_VIEW: tuple[str, ...] = ("未知（没有一致预期、评级、sales note 或大众叙事的材料）",
                                   "未知", "不知道", "无", "unknown", "n/a")

MAX_SENTENCES = 12          # per part
MAX_SENTENCE_CHARS = 600
MAX_UNKNOWNS = 6
MAX_REFS_PER_SENTENCE = 8
MAX_QUERY_CHARS = 200

VERIFICATION_CHECKS: tuple[str, ...] = (
    "cites_only_shown_rows",
    "citations_resolve",
    "numbers_sourced",
    "structure_present",
    "what_happened_cites_evidence",
    "our_view_not_used_as_evidence",
    "reasoning_lint",
    "material_gap_has_request",
    "confidence_in_vocabulary",
    "unknowns_typed",
    "market_vs_us_present",
    "refresh_names_a_capable_source",
    "refusal_states_why",
)
CHECK_LABELS: Mapping[str, str] = {
    "cites_only_shown_rows": "只引用了这次展示过的材料",
    "citations_resolve": "被引原文段落仍能按指纹找回",
    "numbers_sourced": "每个数字都出自所引材料、声明了算式，或标明是我们的估计",
    "structure_present": "回答包含发生了什么、市场原本预期、我们的看法、不确定点四部分",
    "what_happened_cites_evidence": "「发生了什么」引用的是一手材料与行情，不是我们自己的判断",
    "our_view_not_used_as_evidence": "我们自己的判断只出现在「我们的看法」里",
    "reasoning_lint": "推理规范：单一券商不称一致预期；股价归因有基准对照",
    "material_gap_has_request": "材料不足时已登记材料获取请求",
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
    return {"check": check, "label": CHECK_LABELS[check], "status": status,
            "count": len(findings), "findings": [dict(f) for f in findings][:20], "detail": detail}


# ---------------------------------------------------------------------------
# the prompt
# ---------------------------------------------------------------------------

def capability_lines(mission: Mapping[str, Any] | None) -> list[str]:
    status = connection_status(mission)
    lines = []
    for kind in CONTENT_KINDS:
        slugs = sources_for(kind)
        if not slugs:
            continue
        rendered = [f"{slug}{'' if status.get(slug, 'undeclared') == 'connected' else '[' + status.get(slug, 'undeclared') + ']'}"
                    for slug in slugs]
        lines.append(f"- {kind}: " + "、".join(rendered))
    return lines


_SENTENCE_SHAPE = '{"text": "<一句话，句中不写标签>", "refs": ["P3", "M1"], "basis": "verbatim"}'


def build_prompt(context: Mapping[str, Any], *, mission: Mapping[str, Any] | None = None,
                 previous: Mapping[str, Any] | None = None) -> str:
    """The material, the four-part contract, and the citation and number rules."""

    wants_variant = bool(context.get("wants_market_vs_us"))
    shape = [
        "{",
        f' "what_happened": [{_SENTENCE_SHAPE}],',
        f' "market_expected": [{_SENTENCE_SHAPE}],',
        ' "our_view": {"house_view": "<我们当前的看法，一句话>", "change": "strengthens|weakens|no_change|unclear",',
        f'              "sentences": [{_SENTENCE_SHAPE}]}},',
        f' "uncertainties": [{_SENTENCE_SHAPE}],',
        ' "confidence": "high|medium|low",',
        ' "refused": false, "refusal_reason": null, "refusal_detail": null,',
        ' "unknowns": [{"what": "<缺的是什么>", "content_kind": "<下表中的一个词>", "source": "<下表中的一个来源>"}],',
        (' "market_vs_us": {' + ", ".join(f'"{slot}": "..."' for slot in VARIANT_SLOTS)
         + ', "market_view_available": true, "market_view_reason": null, "refs": ["N2"]},'),
        ' "refresh_suggested": {"content_kind": "...", "source": "...", "query": "..."}',
        "}",
    ]
    lines = [
        "你是一位买方分析师。下面是你为这个问题调出来的材料：股价与同业对照（M）、事件（E）、"
        "原文段落（P：公司公告、新闻稿、电话会、券商研报、销售快评）、数字包（N）、我们自己以前的看法（O）、"
        "以及没有原文时才有的旧结论（C）。像读完这些报告的分析师那样回答，形成有价值的判断。",
        *final_text_instructions(),
        "",
        "回答分四部分，顺序固定：",
        "1. what_happened（发生了什么）：只用一手材料与行情写——公司公告/新闻稿/电话会的 P 段落、M 的股价与同业对照、"
        "E 的事件、N 的业绩稿数字。股价变动要同时写同业 basket 或基准的同期表现与超额（M 里有）。"
        "问题所指的那天/那段时间没有直接解释的材料时，如实写清，再写最近一次相关的公司事件。",
        "2. market_expected（市场原本预期什么）：写会前一致预期（只有 N 里来源为一致预期的行才能称「一致预期」）、"
        "券商预览与销售快评里的预期（写出券商名，例如「Goldman Sachs 的会前预览认为…」；单一券商不能称作一致预期或共识）。"
        "没有会前预期材料就写没有，并说缺什么。",
        "3. our_view（我们的看法）：house_view 写我们当前的看法（从 O 的当前论点出发）；change 选 strengthens/weakens/"
        "no_change/unclear，表示这次的材料是加强、削弱、没有改变还是还不能判断这个看法；sentences 用「我们认为……」"
        "说明理由，可以表态。O 里以前的事件研判（包括「维持不变」）只是背景，不能当作证据，也不能复述成结论。",
        "4. uncertainties（不确定点）：写可能改变判断的缺口与待验证的点。不要写「已尝试」「正在获取」——系统会自动补上。",
        "",
        "引用规则：每句的 refs 只能用这次展示过的标签；标签只写在 refs 里，绝不要写进句子正文。"
        "what_happened 和 market_expected 不能引用 O 标签。O 只能在 our_view 里引用。",
        "数字规则：句中每个数字都必须出自该句所引材料——照原文写，或按原文四舍五入/换算单位（例如 $22.2 billion 写成"
        " 222 亿美元）；由几个被引数字算出来的数，basis 填 derivation，并给 \"derivation\": {\"expression\": \"208.36/196.64*100-100\", "
        "\"result\": \"5.96\"}；我们自己的估计必须在句中写「我们的估计」，basis 填 our_estimate。",
        "每条材料的公司归属写在正文之前；涉及某家公司的事实只能引用标为该公司的材料。",
        "用 owner 的语言回答（问题是中文就用中文）。",
        "",
        "只返回一个 JSON 对象，不要 markdown 代码块，形状如下：",
        *shape,
        "",
        "unknowns 里的 content_kind 与 source 必须来自这张表（方括号是这套系统与它的连接状态）：",
        *capability_lines(mission),
        "refresh_suggested：当且仅当材料不足、而上面某个来源能补上时给出；否则填 null。",
        "refused：只有展示的材料完全不能支撑任何回答时才填 true，并从 " + "、".join(REFUSAL_REASONS)
        + " 里选一个 refusal_reason，refusal_detail 写一句人话。",
    ]
    if wants_variant:
        lines += [
            "",
            "这是一个「看法」类问题。market_vs_us 的五格必须每一格都写满："
            + "、".join(f"{slot}（{SLOT_LABELS[slot]}）" for slot in VARIANT_SLOTS)
            + "。market_view 只能来自展示的一致预期、评级、sales note；没有就把 market_view_available 设为 false 并写明缺什么。"
            "where_market_is_wrong 写有证据的假设差异，没有就写没有已证实的分歧。observable_signals 写观察什么、"
            "什么变化会支持或推翻当前判断。",
        ]
    else:
        lines += ["", "这不是看法类问题，market_vs_us 填 null。"]
    lines += ["", "=" * 60, "", render_context(context), "", "=" * 60, ""]
    if previous:
        lines += [
            "这是 owner 对上一个回答的追问。上一轮问答只用来理解问题里的指代（例如「它」「那利润率呢」）；"
            "上一回答引用过的原文段落已经放进上面的 P 段落（标注「上一回答引用过」），可以再次引用。"
            "上一回答本身不是材料；与这次材料不一致时以这次材料为准并说明。",
            f"上一个问题：{previous.get('question') or ''}",
            f"上一个回答：{previous.get('answer') or ''}",
        ]
        cited = [row for row in previous.get("citations") or () if row.get("statement")]
        if cited:
            lines.append("上一个回答所依据的材料（仅供理解，这里的条目不能引用；其中的原文段落要引用只能用上面 P 段落的标签）：")
            lines += [f"- {row['statement']}"
                      + (f"（{'，'.join(x for x in (row.get('company'), row.get('period')) if x)}）"
                         if row.get("company") or row.get("period") else "")
                      for row in cited]
        lines.append("")
    lines.append(f"问题：{context['question']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# numbers
# ---------------------------------------------------------------------------

_SCALE_RE = re.compile(r"([$€£¥]?\s?\d[\d,]*(?:\.\d+)?)\s*(billion|bn|b\b|million|mn|m\b)", re.I)


def _plain_number(value: float) -> str:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text or "0"


def sourced_values(texts: Sequence[str]) -> set[str]:
    """Every figure the cited material states, bare, plus its 亿/万 rescalings."""

    from .mission_deliverable import _sourced_value_set

    found: set[str] = set()
    for text in texts:
        for token in _sourced_value_set(text or ""):
            found.add(token.lstrip("$€£¥").rstrip("%").lstrip("+"))
        for match in _SCALE_RE.finditer(text or ""):
            try:
                value = float(re.sub(r"[^\d.]", "", match.group(1)))
            except ValueError:
                continue
            unit = match.group(2).lower()
            if unit in {"billion", "bn", "b"}:
                found.add(_plain_number(value * 10))          # 亿
                found.add(_plain_number(value * 100000))      # 万
            else:
                found.add(_plain_number(value / 100))         # 亿
                found.add(_plain_number(value * 100))         # 万
        # A raw amount ("USD 18718144000") is written in 亿 / 万 / billion / million.
        for raw in re.findall(r"(?<![\d.])\d{7,15}(?:\.\d+)?(?![\d.])", (text or "").replace(",", "")):
            value = float(raw)
            for divisor in (1e8, 1e4, 1e9, 1e6):
                found.add(_plain_number(value / divisor))
    return {item for item in found if item}


def _rounds_to(token: str, pool: set[str]) -> bool:
    """``token`` is one of the pool's figures as written, or one of them rounded to its precision."""

    bare = token.lstrip("$€£¥+").rstrip("%").replace(",", "")
    if bare in pool:
        return True
    try:
        value = float(bare)
    except ValueError:
        return False
    decimals = len(bare.split(".", 1)[1]) if "." in bare else 0
    for item in pool:
        try:
            source = float(item.replace(",", ""))
        except ValueError:
            continue
        if decimals == 0 and abs(source) < 100:
            continue
        if abs(round(source, decimals) - value) < 10 ** -(decimals + 6) or \
                abs(round(abs(source), decimals) - abs(value)) < 10 ** -(decimals + 6):
            if decimals or abs(source - value) < 1:
                return True
    return False


def _check_numbers(row: Mapping[str, Any], shown: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    from .mission_deliverable import value_tokens

    text = row["text"]
    tokens = value_tokens(text)
    if not tokens:
        return []
    basis = row.get("basis") or "verbatim"
    if basis == "our_estimate":
        if OUR_ESTIMATE_WORDS not in text:
            return [{"code": "estimate_not_labelled", "text": text[:120]}]
        return []
    texts = [str(shown[tag].get("source_text") or shown[tag].get("statement") or "") for tag in row["refs"]]
    if not row["refs"]:
        return [{"code": "number_without_ref", "text": text[:120]}]
    pool = sourced_values(texts)
    if basis == "derivation":
        derivation = row.get("derivation")
        if not isinstance(derivation, Mapping):
            return [{"code": "derivation_missing", "text": text[:120]}]
        from .mission_deliverable import validate_derived_number

        try:
            validate_derived_number({"text": text, "derivation": dict(derivation)}, [{"text": t} for t in texts])
        except Exception as exc:  # noqa: BLE001 - the deliverable rule's own message is the finding
            # A derived figure may round its operands; re-check against the rounded pool.
            result = str(derivation.get("result") or "")
            operands = re.findall(r"\d+(?:\.\d+)?", str(derivation.get("expression") or ""))
            if not (operands and all(_rounds_to(op, pool) or op in {"100", "1000"} or
                                     (op.isdigit() and int(op) <= 12) for op in operands)
                    and result and result.lstrip("-") in text.replace(",", "")):
                return [{"code": "derivation_does_not_check", "text": text[:120], "reason": str(exc)[:160]}]
        return []
    missing = [token for token in tokens if not _rounds_to(token, pool)]
    if missing:
        return [{"code": "number_not_in_cited_material", "text": text[:120], "numbers": missing[:4]}]
    return []


# ---------------------------------------------------------------------------
# reasoning lint
# ---------------------------------------------------------------------------

_LOCAL_RULES: tuple[tuple[str, str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("consensus_without_authority", "「共识/一致预期」必须引用一致预期来源；单一券商写券商名",
     (r"共识|一致预期|consensus|市场预期",),
     (r"某券商|券商认为|据[^。；]{0,10}(?:券商|证券|银行)|买方共识|(?:没有|缺少?|尚无|不可得)[^。；]{0,12}(?:一致预期|共识)",)),
    ("price_move_without_benchmark", "股价归因必须给出基准（大盘/行业/同业 basket）的同期表现",
     (r"股价[^。；]{0,8}(?:上涨|下跌|大涨|大跌|跑赢|跑输|反弹|回撤)|涨幅|跌幅|outperform|underperform",
      r"因为|由于|反映|推动|归因|受[^。；]{0,10}(?:影响|拖累|提振)|驱动"),
     (r"SPY|标普|S&P|指数|行业|同业|basket|相对|超额|基准|SOX|纳指|Nasdaq|大盘",)),
)


def lint_sentence(text: str, *, source_kinds: Sequence[str] = ()) -> list[dict[str, Any]]:
    """D's reasoning lint when this Core has it; otherwise the minimal local set."""

    try:
        from .research_reasoning_lint import lint_text  # type: ignore[import-not-found]
    except ImportError:
        lint_text = None
    if lint_text is not None:
        return [dict(item) for item in lint_text(text, source_kinds=source_kinds)]
    findings = []
    for rule_id, title, triggers, exempt in _LOCAL_RULES:
        if rule_id == "consensus_without_authority" and "consensus" in source_kinds:
            continue
        if not all(re.search(p, text, re.I) for p in triggers):
            continue
        if any(re.search(p, text, re.I) for p in exempt):
            continue
        findings.append({"code": f"lint:{rule_id}", "rule": rule_id, "title": title, "excerpt": text[:160]})
    return findings


# ---------------------------------------------------------------------------
# parsing and verification
# ---------------------------------------------------------------------------

def _rows(value: Any, shown: Mapping[str, Mapping[str, Any]], part: str,
          unshown: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    items = value if isinstance(value, list) else ([value] if isinstance(value, (str, Mapping)) else [])
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
            tag = str(tag).strip().strip("[]")
            if tag in shown:
                if tag not in refs:
                    refs.append(tag)
            elif tag:
                unshown.append(tag)
        basis = item.get("basis") if item.get("basis") in NUMBER_BASES else "verbatim"
        row = {"part": part, "text": text, "refs": refs, "basis": basis}
        if basis == "derivation" and isinstance(item.get("derivation"), Mapping):
            row["derivation"] = {"expression": _text(item["derivation"].get("expression"), 200),
                                 "result": _text(item["derivation"].get("result"), 40)}
        rows.append(row)
    return rows


def parse_answer(
    payload: Any,
    *,
    context: Mapping[str, Any],
    resolve_passage: Callable[[str], Any] | None = None,
    material_requests: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """The model's reply, narrowed to the four-part shape and checked.

    Never raises on a bad reply (a bad answer is shown with its faults named),
    except for a reply that is not an object at all.  ``resolve_passage``
    (``ref -> passage or None``) re-checks every cited passage against the
    index; ``material_requests`` are the requests the cockpit opened for this
    question's missing material.
    """

    if not isinstance(payload, Mapping):
        raise AskAnswerError("模型没有返回一个可用的答案对象")
    shown = {row["tag"]: row for row in context.get("shown") or ()}
    unshown: list[str] = []
    checks: list[dict[str, Any]] = []
    parts: dict[str, list[dict[str, Any]]] = {}
    legacy = not any(key in payload for key in PARTS) and ("sentences" in payload or "answer" in payload)
    if legacy:
        fallback = payload.get("sentences")
        if not fallback and isinstance(payload.get("answer"), str):
            fallback = [{"text": payload["answer"], "refs": []}]
        parts["what_happened"] = _rows(fallback, shown, "what_happened", unshown)
    else:
        parts["what_happened"] = _rows(payload.get("what_happened"), shown, "what_happened", unshown)
    parts["market_expected"] = _rows(payload.get("market_expected"), shown, "market_expected", unshown)
    view = payload.get("our_view") if isinstance(payload.get("our_view"), Mapping) else {}
    parts["our_view"] = _rows(view.get("sentences"), shown, "our_view", unshown)
    parts["uncertainties"] = _rows(payload.get("uncertainties"), shown, "uncertainties", unshown)
    house_view = _text(view.get("house_view"), MAX_SENTENCE_CHARS)
    change = view.get("change") if view.get("change") in VIEW_CHANGES else None
    sentences = [row for part in PARTS for row in parts[part]]

    refused = bool(payload.get("refused"))
    reason = payload.get("refusal_reason")
    if refused and reason not in REFUSAL_REASONS:
        reason = None
    refusal_detail = _text(payload.get("refusal_detail"), 600) or None

    variant, variant_unshown, empty_slots = _market_vs_us(payload.get("market_vs_us"), shown)
    unshown = sorted(set(unshown) | set(variant_unshown))
    checks.append(_result("cites_only_shown_rows", status="fail" if unshown else "pass",
                          findings=[{"code": "unshown_tag", "tag": tag} for tag in unshown],
                          detail=(f"引用全部来自这次展示的 {len(shown)} 条材料" if not unshown
                                  else f"{len(unshown)} 个标签不在展示范围内，已从引用里去掉")))

    cited_tags = list(dict.fromkeys(tag for row in sentences for tag in row["refs"]))
    passage_tags = [tag for tag in cited_tags if shown[tag].get("block") == "passages"]
    broken = []
    if resolve_passage is not None:
        for tag in passage_tags:
            try:
                ok = resolve_passage(str(shown[tag]["ref"])) is not None
            except Exception:  # noqa: BLE001 - a tampered or malformed ref does not resolve
                ok = False
            if not ok:
                broken.append({"code": "passage_does_not_resolve", "tag": tag, "ref": str(shown[tag]["ref"])[:200]})
    checks.append(_result(
        "citations_resolve",
        status=("skipped" if resolve_passage is None or not passage_tags else ("fail" if broken else "pass")),
        findings=broken,
        detail=("没有引用原文段落" if not passage_tags else
                ("没有可用的索引来复核" if resolve_passage is None else
                 (f"{len(passage_tags)} 段被引原文都按指纹找回" if not broken
                  else f"{len(broken)} 段被引原文找不回或指纹不符")))))

    number_findings = [f for row in sentences for f in _check_numbers(row, shown)]
    checks.append(_result("numbers_sourced", status="fail" if number_findings else "pass",
                          findings=number_findings,
                          detail=("每个数字都有出处、算式或「我们的估计」标注" if not number_findings
                                  else f"{len(number_findings)} 句里的数字找不到出处")))

    structure: list[dict[str, Any]] = []
    if not refused:
        if not parts["what_happened"]:
            structure.append({"code": "part_missing", "part": "what_happened"})
        if not parts["market_expected"]:
            structure.append({"code": "part_missing", "part": "market_expected"})
        if not house_view:
            structure.append({"code": "house_view_missing", "part": "our_view"})
        if change is None:
            structure.append({"code": "view_change_not_stated", "part": "our_view"})
        if not parts["uncertainties"] and not payload.get("unknowns") and not material_requests:
            structure.append({"code": "part_missing", "part": "uncertainties"})
    checks.append(_result("structure_present",
                          status="skipped" if refused else ("fail" if structure else "pass"),
                          findings=structure,
                          detail=("拒答" if refused else ("四部分齐全" if not structure else
                                  "缺：" + "、".join(PART_TITLES.get(f["part"], f["part"]) for f in structure)))))

    evidence_findings = []
    first_hand_shown = any(row.get("first_hand") for row in shown.values() if row.get("block") != "house_view")
    for row in parts["what_happened"]:
        if not any(shown[tag].get("evidence") for tag in row["refs"]):
            evidence_findings.append({"code": "what_happened_without_evidence", "text": row["text"][:120]})
    if parts["what_happened"] and first_hand_shown and not any(
            shown[tag].get("first_hand") for row in parts["what_happened"] for tag in row["refs"]):
        evidence_findings.append({"code": "first_hand_material_not_cited"})
    checks.append(_result("what_happened_cites_evidence",
                          status="skipped" if refused or not parts["what_happened"] else
                          ("fail" if evidence_findings else "pass"),
                          findings=evidence_findings,
                          detail=("「发生了什么」每句都引了一手材料或行情" if not evidence_findings
                                  else f"{len(evidence_findings)} 处「发生了什么」没有一手出处")))

    misuse = []
    for part in ("what_happened", "market_expected"):
        for row in parts[part]:
            if any(shown[tag].get("block") == "house_view" for tag in row["refs"]):
                misuse.append({"code": "our_view_cited_as_evidence", "part": part, "text": row["text"][:120]})
            elif re.search(r"NO_CHANGE|维持(?:原|两条|投资)?(?:论点|判断|假设)[^。；]{0,10}不变|我们(?:此前|之前)?的(?:事件)?研判", row["text"]):
                misuse.append({"code": "house_judgement_restated_as_evidence", "part": part, "text": row["text"][:120]})
    checks.append(_result("our_view_not_used_as_evidence", status="fail" if misuse else "pass",
                          findings=misuse,
                          detail=("我们自己的判断只出现在「我们的看法」" if not misuse
                                  else "我们自己的判断被当成了证据")))

    lint: list[dict[str, Any]] = []
    for row in sentences:
        kinds = sorted({str(shown[tag].get("source")) for tag in row["refs"] if shown[tag].get("source")})
        for finding in lint_sentence(row["text"], source_kinds=kinds):
            lint.append({**finding, "part": row["part"]})
    checks.append(_result("reasoning_lint", status="fail" if lint else "pass", findings=lint,
                          detail="推理规范检查通过" if not lint else f"{len(lint)} 句违反推理规范"))

    sufficiency = context.get("sufficiency") or {}
    needs_request = bool(sufficiency.get("request"))
    has_request = bool([r for r in material_requests or () if r.get("id")])
    checks.append(_result("material_gap_has_request",
                          status=("skipped" if not needs_request else ("pass" if has_request else "fail")),
                          findings=([] if not needs_request or has_request else
                                    [{"code": "gap_without_request", "reason": sufficiency.get("reason")}]),
                          detail=("材料充分，不需要补取" if not needs_request else
                                  ("已登记材料获取请求" if has_request else "材料不足但没有登记获取请求"))))

    confidence = payload.get("confidence")
    valid_confidence = confidence in CONFIDENCE
    checks.append(_result("confidence_in_vocabulary", status="pass" if valid_confidence else "fail",
                          findings=([] if valid_confidence else
                                    [{"code": "confidence_not_in_vocabulary", "value": str(confidence)[:40]}]),
                          detail=(f"信心：{confidence}" if valid_confidence else "没有给出 high / medium / low 之一，按 low 记")))
    if not valid_confidence:
        confidence = "low"

    unknowns, bad_unknowns = _unknowns(payload.get("unknowns"))
    checks.append(_result("unknowns_typed", status="fail" if bad_unknowns else "pass", findings=bad_unknowns,
                          detail=(f"{len(unknowns)} 个缺口都点名了内容类型与来源" if not bad_unknowns
                                  else f"{len(bad_unknowns)} 个缺口没有说清缺什么或去哪取")))

    wants_variant = bool(context.get("wants_market_vs_us"))
    variant_findings: list[dict[str, Any]] = []
    if wants_variant and not refused:
        if variant is None:
            variant_findings.append({"code": "market_vs_us_missing"})
        else:
            variant_findings += [{"code": "market_vs_us_slot_empty", "slot": slot} for slot in empty_slots]
    checks.append(_result(
        "market_vs_us_present",
        status="fail" if variant_findings else ("pass" if wants_variant else "skipped"),
        findings=variant_findings,
        detail=("这不是看法类问题" if not wants_variant else
                ("写了我们与市场的差别与收敛路径" if not variant_findings else
                 ("看法类问题没有写我们与市场的差别" if variant is None else
                  "这几格是空的：" + "、".join(SLOT_LABELS.get(s, s) for s in empty_slots))))))

    refresh, refresh_findings = _refresh(payload.get("refresh_suggested"))
    checks.append(_result("refresh_names_a_capable_source",
                          status="fail" if refresh_findings else ("pass" if refresh else "skipped"),
                          findings=refresh_findings,
                          detail=("没有建议补搜" if refresh is None and not refresh_findings else
                                  (f"建议去 {refresh['source']} 取 {refresh['content_kind']}" if refresh
                                   else "建议的补搜指向一个给不出那类内容的来源"))))

    checks.append(_result(
        "refusal_states_why",
        status="fail" if (refused and (reason is None or refusal_detail is None)) else "pass",
        findings=([] if not refused or (reason and refusal_detail) else
                  [{"code": "refusal_without_reason", "reason": str(payload.get("refusal_reason"))[:40]}]),
        detail=("没有拒答" if not refused else
                (f"拒答理由：{REFUSAL_LABELS.get(reason, reason)}" if reason else "拒答但没有给出封闭词表里的理由"))))

    our_view = {"house_view": house_view or None, "change": change,
                "change_label": VIEW_CHANGE_LABELS.get(change) if change else None}
    sections = assemble_sections(parts, our_view=our_view, variant=variant,
                                 material_requests=material_requests or (),
                                 tried=(context.get("sufficiency") or {}).get("tried"))
    body = "\n\n".join(section["text"] for section in sections if section["text"])
    if legacy:
        # A pre-batch-G reply (one flat list) keeps the body it always had, so
        # an answer recovered from its stored reply reads -- and hashes -- the same.
        body = assemble_body(parts["what_happened"], variant)
    citations = []
    seen: set[str] = set()
    for tag in [*cited_tags, *((variant or {}).get("refs") or ())]:
        if tag in shown and tag not in seen:
            seen.add(tag)
            citations.append(dict(shown[tag]))
    findings = [f for check in checks for f in check["findings"]]
    gaps = [row["text"] for row in parts["uncertainties"]] + [
        f"{item['what']}（{item['content_kind']} → {item['source']}）" for item in unknowns]
    return {
        "schema_version": SCHEMA_VERSION,
        "answer": body,
        "sections": sections,
        "sentences": sentences,
        "our_view": our_view,
        "citations": citations,
        "cited_tags": [row["tag"] for row in citations],
        "confidence": confidence,
        "refused": refused,
        "refusal_reason": reason,
        "refusal_label": REFUSAL_LABELS.get(reason) if reason else None,
        "refusal_detail": refusal_detail,
        "unknowns": unknowns,
        "gaps": gaps,
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


def material_lines(material_requests: Sequence[Mapping[str, Any]], tried: Any = None) -> list[str]:
    """「已尝试：…」 and 「正在获取：…」, written by the system, never by the model."""

    lines = []
    if tried and any(item.get("id") for item in material_requests):
        lines.append(f"已尝试：{tried}")
    for item in material_requests:
        if not item.get("id"):
            continue
        status = item.get("status_label") or item.get("status") or "待获取"
        what = item.get("what") or (item.get("need") or {}).get("question") or "补充材料"
        word = "正在获取" if item.get("status") in (None, "open", "partial") else status
        code = str(item["id"]).rsplit(":", 1)[-1][:8]
        lines.append(f"{word}：{what}（材料请求编号 {code}，状态：{status}）")
    return lines


def assemble_sections(parts: Mapping[str, Sequence[Mapping[str, Any]]], *, our_view: Mapping[str, Any],
                      variant: Mapping[str, Any] | None, material_requests: Sequence[Mapping[str, Any]] = (),
                      tried: Any = None) -> list[dict[str, Any]]:
    """The four parts as the owner reads them, assembled here (tags never enter the prose)."""

    sections = []
    for part in PARTS:
        rows = list(parts.get(part) or ())
        lines = [" ".join(row["text"] for row in rows).strip()] if rows else []
        extra: list[str] = []
        if part == "our_view":
            head = []
            if our_view.get("house_view"):
                head.append(f"当前看法：{our_view['house_view']}")
            if our_view.get("change_label"):
                head.append(f"是否改变：{our_view['change_label']}。")
            lines = ["".join(head)] + lines if head else lines
            if variant is not None:
                for slot in VARIANT_SLOTS:
                    value = variant.get(slot)
                    if value:
                        extra.append(f"{SLOT_LABELS[slot]}：{value}"
                                     + (f"（{variant.get('market_view_reason')}）"
                                        if slot == "market_view" and not variant.get("market_view_available")
                                        and variant.get("market_view_reason") else ""))
        if part == "uncertainties":
            extra += material_lines(material_requests, tried)
        lines = [line for line in [*lines, *extra] if line]
        text = (f"{PART_TITLES[part]}\n" + "\n".join(lines)) if lines else ""
        sections.append({"key": part, "title": PART_TITLES[part], "text": text,
                         "body": "\n".join(lines), "sentences": [dict(row) for row in rows],
                         "system_lines": extra if part == "uncertainties" else []})
    return sections


def assemble_body(sentences: Sequence[Mapping[str, Any]], variant: Mapping[str, Any] | None) -> str:
    """Older callers: the sentences as one paragraph, the variant slots after it."""

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


def _unknowns(value: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
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
            bad.append({"code": "unknown_content_kind", "what": what[:120], "content_kind": kind[:40]})
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


def _market_vs_us(value: Any, shown: Mapping[str, Mapping[str, Any]]
                  ) -> tuple[dict[str, Any] | None, list[str], list[str]]:
    if not isinstance(value, Mapping):
        return None, [], list(VARIANT_SLOTS)
    slots = {slot: _text(value.get(slot), MAX_SENTENCE_CHARS) for slot in VARIANT_SLOTS}
    available = value.get("market_view_available")
    if not isinstance(available, bool):
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
    return ({**slots, "market_view_available": available, "market_view_reason": reason, "refs": refs},
            unshown, empty)


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
        return None, [{"code": "source_cannot_answer_content_kind", "content_kind": kind, "source": source}]
    if not query:
        return None, [{"code": "refresh_without_a_query"}]
    return {"content_kind": kind, "source": source, "query": query}, []


__all__ = [
    "AskAnswerError", "CHECK_LABELS", "CONFIDENCE", "MAX_UNKNOWNS", "NO_MARKET_VIEW", "NUMBER_BASES",
    "PARTS", "PART_TITLES", "REFUSAL_LABELS", "REFUSAL_REASONS", "SCHEMA_VERSION", "SLOT_LABELS",
    "VERIFICATION_CHECKS", "VIEW_CHANGES", "VIEW_CHANGE_LABELS", "VIEW_KINDS", "assemble_body",
    "assemble_sections", "build_prompt", "capability_lines", "lint_sentence", "material_lines",
    "parse_answer", "sourced_values",
]
