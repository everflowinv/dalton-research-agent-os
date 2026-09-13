"""One-pass language review and one-pass brain revision before publication."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from hashlib import sha256
from typing import Any

from .cockpit_model import register_purpose
from .final_text_contract import final_text_instructions
from .research_localization import validate_localized_text

SCHEMA_VERSION = "research-language-review:0.1"
CHECKER_PURPOSE = register_purpose("research_language_check")
BRAIN_PURPOSE = register_purpose("research_language_revision")
CHECKER_PROVIDER = "antigravity-cli-gateway"
CHECKER_MODEL = "antigravity-cli-gateway/gemini-3.8-flash"


class ResearchLanguageReviewError(ValueError):
    pass


def parse_stage_output(text: str, *, stage: str) -> dict[str, Any]:
    """Recover one complete closed object from a restarted text stream.

    Some transports retain an incomplete prefix before the model restarts its
    JSON response. Keep the original call bytes, accept only one unambiguous
    complete stage object, and leave all content validation to the caller.
    """
    keys = {'draft': {'sections'}, 'checker': {'overall', 'suggestions'},
            'brain': {'decisions', 'sections'}}
    if stage not in keys:
        raise ValueError('unknown language review stage')
    decoder = json.JSONDecoder()
    candidates = {}
    for index, char in enumerate(text):
        if char != '{':
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and set(value) == keys[stage]:
            candidates[_hash(value)] = value
    if len(candidates) != 1:
        raise ResearchLanguageReviewError('language stage has no unique complete JSON object')
    return next(iter(candidates.values()))


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode()


def _hash(value: Any) -> str:
    return sha256(_canonical(value)).hexdigest()


def build_checker_prompt(product: Mapping[str, Any]) -> str:
    sections = [{"index": i, "title": row.get("title") or "",
                 "body": row.get("body") or "", "gaps": row.get("gaps") or []}
                for i, row in enumerate(product.get("sections") or [])]
    return "\n".join((
        "你是中文投研成品的语言检查员。逐句判断一位有正常文化程度、具备基础金融知识的读者，"
        "能否轻松读懂这份简体中文材料。只检查语言，不核实事实、数字、来源或投资结论。",
        *final_text_instructions(),
        "指出生硬机翻、语法病句、含混指代、无必要的工程黑话、重复防御句和中英文混排问题。"
        "保留专有名词、原文引用；Excel 标题和文字说明需要检查，公式与数据单元格不在范围内，"
        "Excel 正文可保留英文。每条建议必须对应一个 section 和原句，不得提出新事实或新数字。",
        "只输出 JSON："
        '{"overall":"一句话", "suggestions":[{"section_index":0,"quote":"原句",'
        '"assessment":"为何难懂","suggestion":"建议表述"}]}',
        "原文 SHA-256：" + _hash(product),
        "待检查章节：" + json.dumps(sections, ensure_ascii=False, separators=(",", ":")),
    ))


def validate_checker_output(value: Mapping[str, Any], *, sections: list[Mapping[str, Any]]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"overall", "suggestions"}:
        raise ResearchLanguageReviewError("language checker output has an invalid shape")
    if not isinstance(value["overall"], str) or not value["overall"].strip():
        raise ResearchLanguageReviewError("language checker overall assessment is missing")
    if not isinstance(value["suggestions"], list):
        raise ResearchLanguageReviewError("language checker suggestions must be a list")
    suggestions = []
    for item in value["suggestions"]:
        required = {"section_index", "quote", "assessment", "suggestion"}
        body_alias = {"section_index", "quote", "body", "suggestion"}
        if not isinstance(item, Mapping):
            raise ResearchLanguageReviewError("language checker suggestion has an invalid shape")
        item = dict(item)
        # One reviewed checker transport emitted its language assessment under
        # ``body``. Accept only that exact closed alias; mixed or extra fields
        # remain invalid, and quote anchoring below is unchanged.
        optional = {"title", "index"}
        if (set(item) - optional == body_alias
                and isinstance(item.get("body"), str) and item["body"].strip()):
            item["assessment"] = item.pop("body")
        if set(item) - optional != required:
            raise ResearchLanguageReviewError("language checker suggestion has an invalid shape")
        index = item["section_index"]
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(sections):
            raise ResearchLanguageReviewError("language checker section index is invalid")
        if 'index' in item:
            if (isinstance(item['index'], bool) or not isinstance(item['index'], int)
                    or item['index'] != index):
                raise ResearchLanguageReviewError("language checker redundant index differs from section index")
            del item['index']
        if 'title' in item and item['title'] != sections[index].get('title'):
            raise ResearchLanguageReviewError("language checker redundant title differs from source")
        if any(not isinstance(item[key], str) for key in ('quote', 'assessment', 'suggestion')):
            raise ResearchLanguageReviewError("language checker suggestion text must be a string")
        fields = {key: str(item[key]).strip() for key in ("quote", "assessment", "suggestion")}
        if any(not text for text in fields.values()):
            raise ResearchLanguageReviewError("language checker suggestion text is missing")
        def source_text(source: Mapping[str, Any]) -> str:
            return "\n".join((str(source.get("title") or ""),
                              str(source.get("body") or ""),
                              *(str(x) for x in source.get("gaps") or [])))
        def visible_quote(text: str) -> str:
            text = re.sub(r'\\u(?:200[bcd]|feff)', '', text, flags=re.IGNORECASE)
            return text.translate({ord(char): None for char in '\u200b\u200c\u200d\ufeff'})
        visible = visible_quote(fields['quote'])
        # A terminal ellipsis denotes an excerpt, never a wildcard inside it.
        excerpt = re.sub(r'(?:\u2026+|\.{3,})$', '', visible).rstrip()
        def matches(source: Mapping[str, Any]) -> bool:
            text = visible_quote(source_text(source))
            return bool(excerpt.strip()) and (visible in text or excerpt in text)
        if not matches(sections[index]):
            # A checker may cite the right sentence with the wrong chapter
            # number. Re-anchor only an exact, uniquely located excerpt.
            locations = [i for i, source in enumerate(sections) if matches(source)]
            if len(locations) == 1:
                index = locations[0]
            else:
                raise ResearchLanguageReviewError("language checker quote is not in its source section")
        if not visible.strip():
            raise ResearchLanguageReviewError("language checker quote is not in its source section")
        suggestions.append({"section_index": index, **fields})
    return {"overall": value["overall"].strip(), "suggestions": suggestions}


def render_suggestions_markdown(review: Mapping[str, Any]) -> str:
    lines = ["# 发布前语言检查建议", "",
             "> 本检查只评估中文可读性，不核实事实、数字、来源或投资结论。", "",
             str(review["overall"]), ""]
    if not review["suggestions"]:
        lines.append("- 未发现需要修改的语言问题。")
    for index, item in enumerate(review["suggestions"], 1):
        lines.extend((f"## 建议 {index}（章节 {item['section_index']}）", "",
                      f"- 原句：{item['quote']}", f"- 问题：{item['assessment']}",
                      f"- 建议：{item['suggestion']}", ""))
    return "\n".join(lines).rstrip() + "\n"


def build_brain_prompt(product: Mapping[str, Any], review: Mapping[str, Any]) -> str:
    source = {
        "kind": product.get("kind"), "version_ref": product.get("version_ref"),
        "source_hash": _hash(product),
        "sections": [{"index": index, "title": row.get("title") or "",
                      "body": row.get("body") or "", "gaps": row.get("gaps") or []}
                     for index, row in enumerate(product.get("sections") or [])],
    }
    return "\n".join((
        "你是负责该研究成品的大脑。语言检查员只评估表达，没有核实事实或数字。逐条决定采纳或拒绝，"
        "给出具体理由，然后返回修订后的全部章节。只改语言；不得新增、删除或改变事实、数字、来源、"
        "审批状态和章节结构。不要再次要求语言检查。",
        *final_text_instructions(),
        "只输出 JSON："
        '{"decisions":[{"suggestion_index":0,"decision":"adopt|reject","reason":"理由"}],'
        '"sections":[{"index":0,"title":"...","body":"...","gaps":[]}]}',
        "原文展示字段与不可变身份：" + json.dumps(
            source, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        "语言建议：" + json.dumps(review, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    ))


def _validate_brain_output(product: Mapping[str, Any], review: Mapping[str, Any],
                           value: Mapping[str, Any], *,
                           numeric_source_product: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"decisions", "sections"}:
        raise ResearchLanguageReviewError("brain revision output has an invalid shape")
    decisions = value["decisions"]
    if not isinstance(decisions, list) or len(decisions) != len(review["suggestions"]):
        raise ResearchLanguageReviewError("brain must decide every language suggestion exactly once")
    checked_decisions = []
    seen = set()
    for item in decisions:
        if not isinstance(item, Mapping) or set(item) != {"suggestion_index", "decision", "reason"}:
            raise ResearchLanguageReviewError("brain decision has an invalid shape")
        index = item["suggestion_index"]
        if (isinstance(index, bool) or not isinstance(index, int)
                or not 0 <= index < len(decisions) or index in seen):
            raise ResearchLanguageReviewError("brain suggestion decision index is invalid")
        if item["decision"] not in {"adopt", "reject"}:
            raise ResearchLanguageReviewError("brain decision must be adopt or reject")
        if not isinstance(item["reason"], str) or not item["reason"].strip():
            raise ResearchLanguageReviewError("brain decision reason is missing")
        seen.add(index)
        checked_decisions.append({"suggestion_index": index, "decision": item["decision"],
                                  "reason": item["reason"].strip()})
    localized = {"sections": value["sections"]}
    checked_sections = validate_localized_text(
        numeric_source_product if numeric_source_product is not None else product,
        localized,
    )
    return {"decisions": checked_decisions,
            "sections": [{"index": row["index"], "title": row["title"],
                           "body": row["body"], "gaps": row["gaps"]}
                          for row in checked_sections]}


def run_language_review(
    product: Mapping[str, Any], *,
    checker: Callable[[str], Mapping[str, Any]],
    brain: Callable[[str], Mapping[str, Any]],
    checker_identity: Mapping[str, str],
    numeric_source_product: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Call the checker once, then the brain once; fail closed without retry loops."""

    source_hash = _hash(product)
    numeric_source_hash = None
    if numeric_source_product is not None:
        if (numeric_source_product.get("kind") != product.get("kind")
                or numeric_source_product.get("version_ref") != product.get("version_ref")
                or len(numeric_source_product.get("sections") or [])
                    != len(product.get("sections") or [])):
            raise ResearchLanguageReviewError(
                "numeric source identity or section count differs from reviewed product")
        validate_localized_text(
            numeric_source_product,
            {"sections": list(product.get("sections") or [])},
        )
        numeric_source_hash = _hash(numeric_source_product)
    if dict(checker_identity) != {"provider": CHECKER_PROVIDER, "model": CHECKER_MODEL}:
        raise ResearchLanguageReviewError(
            "language checker must use the exact Antigravity Gemini 3.8 Flash transport identity")
    try:
        review = validate_checker_output(
            checker(build_checker_prompt(product)),
            sections=list(product.get("sections") or []),
        )
    except Exception as exc:
        result = {"schema_version": SCHEMA_VERSION, "status": "pending_language_review",
                  "source_hash": source_hash, "reason": str(exc)}
        if numeric_source_hash is not None:
            result["numeric_source_hash"] = numeric_source_hash
        return result
    markdown = render_suggestions_markdown(review)
    try:
        revision = _validate_brain_output(
            product, review, brain(build_brain_prompt(product, review)),
            numeric_source_product=numeric_source_product,
        )
    except Exception as exc:
        result = {"schema_version": SCHEMA_VERSION, "status": "pending_brain_revision",
                  "source_hash": source_hash, "checker_identity": dict(checker_identity),
                  "language_review": review, "suggestions_markdown": markdown,
                  "reason": str(exc)}
        if numeric_source_hash is not None:
            result["numeric_source_hash"] = numeric_source_hash
        return result
    result = {"schema_version": SCHEMA_VERSION, "status": "ready_for_publication",
              "source_hash": source_hash, "checker_identity": dict(checker_identity),
              "language_review": review, "suggestions_markdown": markdown,
              "brain_revision": revision, "revision_hash": _hash(revision["sections"]),
              "language_scope": "readability_only_not_fact_or_number_verification"}
    if numeric_source_hash is not None:
        result["numeric_source_hash"] = numeric_source_hash
    result["content_hash"] = _hash(result)
    return result


def publish_language_attachment(
    product: Mapping[str, Any], *,
    checker: Callable[[str], Mapping[str, Any]],
    brain: Callable[[str], Mapping[str, Any]],
    checker_identity: Mapping[str, str],
    save_review: Callable[[Mapping[str, Any]], Any],
    publish_attachment: Callable[[Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]],
) -> dict[str, Any]:
    """Run the gate, retain its result, and publish only a passing attachment.

    ``save_review`` is called for every terminal attempt, including pending
    failures. ``publish_attachment`` is called only once for a ready result and
    receives the unchanged source product plus the bound review result.
    """

    result = run_language_review(
        product, checker=checker, brain=brain, checker_identity=checker_identity)
    save_review(result)
    if result["status"] != "ready_for_publication":
        return result
    published = publish_attachment(product, result)
    if not isinstance(published, Mapping):
        raise ResearchLanguageReviewError("language attachment publisher returned no receipt")
    return {**result, "attachment": dict(published)}


__all__ = ["BRAIN_PURPOSE", "CHECKER_MODEL", "CHECKER_PROVIDER", "CHECKER_PURPOSE",
           "ResearchLanguageReviewError",
           "SCHEMA_VERSION", "build_brain_prompt", "build_checker_prompt",
           "publish_language_attachment", "render_suggestions_markdown", "run_language_review",
           "validate_checker_output"]
