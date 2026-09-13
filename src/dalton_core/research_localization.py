"""Hash-bound, rebuildable Chinese display attachments for research products.

The attachment is presentation data.  It never replaces the research product,
its approval state, source refs, figures or version identity.
"""

from __future__ import annotations

import copy
import json
import re
from collections import Counter
from hashlib import sha256
from typing import Any, Mapping, Sequence

from .final_text_contract import FINAL_TEXT_RULES_VERSION, final_text_instructions
from .numeric_display import format_display_number

SCHEMA_VERSION = "research-localization:0.1"
TARGET_LOCALE = "zh-CN"
_NUMBER = re.compile(r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?")
_OPAQUE_ID = re.compile(
    r"\b(?:claim|claim-version|dossier|dossier-version|memo|memo-version|"
    r"debate|debate-map|forecast-model-version|company-model-spec|mission|"
    r"mission-version|thesis|thesis-version|event|document|document-version)"
    r":[A-Za-z0-9:._-]+|\b[0-9a-f]{64}\b",
    re.IGNORECASE,
)
_HAN = re.compile(r"[\u3400-\u9fff]")
_ENGLISH_NUMBER_VALUES = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "ten": "10", "eleven": "11", "twelve": "12",
    "january": "1", "february": "2", "march": "3", "april": "4",
    "may": "5", "june": "6", "july": "7", "august": "8",
    "september": "9", "october": "10", "november": "11", "december": "12",
}
_ENGLISH_NUMBER = re.compile(
    r"\b(" + "|".join(_ENGLISH_NUMBER_VALUES) + r")\b", re.IGNORECASE
)


class ResearchLocalizationError(ValueError):
    """A display attachment is malformed or no longer matches its source."""


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode()


def _hash(value: Any) -> str:
    return sha256(_canonical(value)).hexdigest()


def source_content_hash(product: Mapping[str, Any]) -> str:
    return _hash(product)


def _text_hash(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _numbers(*values: str) -> list[str]:
    return sorted(token for value in values
                  for token in _NUMBER.findall(_OPAQUE_ID.sub("", value)))


def _number_differences(source_values: Sequence[str], target_values: Sequence[str]) -> tuple[list[str], list[str]]:
    """Return lost Arabic tokens and genuinely new target Arabic tokens.

    English number words and month names may become Arabic digits in otherwise
    Chinese prose. They may explain only an added target token; an Arabic token
    present in the source must still survive exactly.
    """

    source_numbers = Counter(_numbers(*source_values))
    target_numbers = Counter(_numbers(*target_values))
    missing_counter = source_numbers - target_numbers
    added = target_numbers - source_numbers
    aliases: Counter[str] = Counter()
    for value in source_values:
        cleaned = _OPAQUE_ID.sub("", value)
        aliases.update(_ENGLISH_NUMBER_VALUES[m.group(1).lower()]
                       for m in _ENGLISH_NUMBER.finditer(cleaned))
    added -= aliases

    source_joined = " ".join(source_values)
    target_compact = re.sub(r"\s+", "", " ".join(target_values))
    # Consume only a formatting result computed from the exact missing source
    # token and an explicit source unit. Unlabelled numbers remain strict.
    for token in list(missing_counter.elements()):
        numeric_token = token[:-1] if token.endswith("%") else token
        escaped = re.escape(numeric_token)
        kinds: list[str] = []
        if (re.search(rf"(?<![\d.]){escaped}\s*(?:USD|美元)\b", source_joined, re.I)
                or re.search(rf"\bUSD\s*{escaped}(?![\d.])", source_joined, re.I)):
            kinds.append("amount_usd")
        if re.search(rf"(?<![\d.]){escaped}\s*%", source_joined):
            kinds.append("percent")
        if re.search(rf"(?:EPS|每股收益)[^\d]{{0,20}}{escaped}|{escaped}[^\d]{{0,12}}(?:EPS|每股)",
                     source_joined, re.I):
            kinds.append("eps")
        if re.search(rf"ARPU[^\d]{{0,20}}{escaped}|{escaped}[^\d]{{0,12}}ARPU",
                     source_joined, re.I):
            kinds.append("arpu")
        for kind in kinds:
            rendered = format_display_number(numeric_token, kind=kind)  # type: ignore[arg-type]
            rendered_compact = rendered.replace(" ", "")
            rendered_numbers = _numbers(rendered)
            if rendered_compact in target_compact and all(added[number] for number in rendered_numbers):
                missing_counter[token] -= 1
                for number in rendered_numbers:
                    added[number] -= 1
                break
    return sorted(missing_counter.elements()), sorted(added.elements())


def build_prompt(product: Mapping[str, Any]) -> str:
    """Build a translation/edit prompt without granting new research work."""

    sections = [
        {"index": i, "title": str(row.get("title") or ""),
         "body": str(row.get("body") or ""),
         "gaps": [str(x) for x in row.get("gaps") or []]}
        for i, row in enumerate(product.get("sections") or [])
    ]
    shape = {"sections": [
        {"index": row["index"], "title": "通顺的简体中文标题",
         "body": "忠实、简洁的简体中文正文", "gaps": ["真正影响判断的缺口"]}
        for row in sections
    ]}
    return "\n".join((
        "Translate and edit an existing research product for Chinese display. This is a faithful "
        "presentation pass, not new research.",
        *final_text_instructions(),
        "Keep exactly one output section for each input section, in the same order and with the "
        "same index. You may merge repetitive defensive sentences inside a section and repair "
        "awkward wording, but retain every uncertainty that could change the judgement.",
        "Preserve every authoritative value. You may format an explicit USD amount into 万美元 or "
        "亿美元 with normal display rounding, a percentage to one decimal place, and an explicitly "
        "labelled EPS or ARPU to two decimal places. Do not change the underlying value or unit. "
        "English number words and month "
        "names may be translated as Chinese written numerals (for example, one→一 and "
        "December→十二月); do not introduce Arabic digits for them unless needed. Do not "
        "translate, summarize or reproduce "
        "source refs; they remain attached from the source product outside this response.",
        "Return raw JSON only with exactly the requested shape.",
        "SOURCE PRODUCT SHA-256: " + source_content_hash(product),
        "SOURCE SECTIONS: " + json.dumps(sections, ensure_ascii=False, separators=(",", ":")),
        "OUTPUT SHAPE: " + json.dumps(shape, ensure_ascii=False, separators=(",", ":")),
    ))


def build_verifier_prompt(product: Mapping[str, Any], localized: Mapping[str, Any]) -> str:
    source = {
        "kind": product.get("kind"), "version_ref": product.get("version_ref"),
        "content_hash": source_content_hash(product),
        "sections": [{"index": index, "title": row.get("title") or "",
                      "body": row.get("body") or "", "gaps": row.get("gaps") or []}
                     for index, row in enumerate(product.get("sections") or [])],
    }
    return "\n".join((
        "You are an independent verifier of a Chinese presentation attachment for an existing "
        "research product.",
        "Check faithful meaning, fluent Simplified Chinese, exact section order, preservation of "
        "all decision-relevant uncertainties and all financial number tokens, and absence of new "
        "facts, sources, approvals or actions. Proper nouns and verbatim quotations may remain in "
        "their source language.",
        "Return raw JSON only: "
        '{"verdict":"pass|reject","faithful":true,"no_new_facts":true,'
        '"meaning_preserved":true,"findings":["具体问题"]}',
        "Source refs, numbers, status, version and approval remain bound by the source product "
        "outside this presentation attachment and must not be changed or translated.",
        "SOURCE: " + json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        "LOCALIZED: " + json.dumps(localized, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    ))


def validate_localized_text(product: Mapping[str, Any], localized: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Cheap structural and numeric preservation checks to run before verification."""

    sections = product.get("sections") or []
    rows = localized.get("sections") if isinstance(localized, Mapping) else None
    if not isinstance(rows, list) or len(rows) != len(sections):
        raise ResearchLocalizationError("localized sections must match source section count")
    checked = []
    for index, (source, row) in enumerate(zip(sections, rows)):
        if not isinstance(source, Mapping) or not isinstance(row, Mapping):
            raise ResearchLocalizationError("sections must be objects")
        if set(row) != {"index", "title", "body", "gaps"} or row["index"] != index:
            raise ResearchLocalizationError("localized section shape or order is invalid")
        if not isinstance(row["title"], str) or not row["title"].strip() or not isinstance(row["body"], str):
            raise ResearchLocalizationError("localized title/body must be text")
        if not isinstance(row["gaps"], list) or any(not isinstance(x, str) for x in row["gaps"]):
            raise ResearchLocalizationError("localized gaps must be text")
        source_text = (str(source.get("title") or ""), str(source.get("body") or ""),
                       *(str(x) for x in source.get("gaps") or []))
        target_text = (row["title"], row["body"], *(str(x) for x in row["gaps"]))
        missing, added = _number_differences(source_text, target_text)
        if missing or added:
            detail = {"section_index": index, "missing_tokens": missing,
                      "added_tokens": added}
            raise ResearchLocalizationError(
                "localized section changed financial number tokens: "
                + json.dumps(detail, ensure_ascii=False, sort_keys=True)
            )
        if any(value.strip() for value in source_text) and not _HAN.search(" ".join(target_text)):
            raise ResearchLocalizationError(
                f"localized section {index} has no Simplified Chinese presentation text"
            )
        checked.append({
            "index": index,
            "source_title_sha256": _text_hash(source_text[0]),
            "source_body_sha256": _text_hash(source_text[1]),
            "source_gaps_sha256": _hash(list(source.get("gaps") or [])),
            "title": row["title"].strip(), "body": row["body"].strip(),
            "gaps": [x.strip() for x in row["gaps"]],
        })
    return checked


def build_localization(product: Mapping[str, Any], localized: Mapping[str, Any],
                       verifier: Mapping[str, Any]) -> dict[str, Any]:
    checked = validate_localized_text(product, localized)
    expected_verifier = {"verdict", "faithful", "no_new_facts", "meaning_preserved", "findings"}
    if (not isinstance(verifier, Mapping) or set(verifier) != expected_verifier
            or verifier.get("verdict") != "pass"
            or any(verifier.get(key) is not True for key in
                   ("faithful", "no_new_facts", "meaning_preserved"))
            or not isinstance(verifier.get("findings"), list)
            or verifier.get("findings")):
        raise ResearchLocalizationError("independent localization verifier did not pass cleanly")
    candidate = {
        "schema_version": SCHEMA_VERSION, "status": "localized",
        "target_locale": TARGET_LOCALE, "rules_version": FINAL_TEXT_RULES_VERSION,
        "source": {
            "kind": product.get("kind"), "version_ref": product.get("version_ref"),
            "content_hash": source_content_hash(product),
        },
        "sections": checked,
        "verifier": dict(verifier),
    }
    candidate["content_hash"] = _hash(candidate)
    return candidate


def validate_localization(product: Mapping[str, Any], candidate: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(candidate, Mapping):
        raise ResearchLocalizationError("localization must be an object")
    body = dict(candidate)
    digest = body.pop("content_hash", None)
    if digest != _hash(body):
        raise ResearchLocalizationError("localization content hash mismatch")
    if set(candidate) != {"schema_version", "status", "target_locale", "rules_version",
                          "source", "sections", "verifier", "content_hash"}:
        raise ResearchLocalizationError("localization has an open or incomplete shape")
    if (candidate["schema_version"] != SCHEMA_VERSION or candidate["status"] != "localized"
            or candidate["target_locale"] != TARGET_LOCALE
            or candidate["rules_version"] != FINAL_TEXT_RULES_VERSION):
        raise ResearchLocalizationError("localization identity is unsupported")
    source = candidate["source"]
    if source != {"kind": product.get("kind"), "version_ref": product.get("version_ref"),
                   "content_hash": source_content_hash(product)}:
        raise ResearchLocalizationError("localization source identity mismatch")
    # Rebuild from its own display fields to replay all hashes and verifier gates.
    localized = {"sections": [{"index": x["index"], "title": x["title"],
                                "body": x["body"], "gaps": x["gaps"]}
                               for x in candidate["sections"]]}
    if build_localization(product, localized, candidate["verifier"]) != dict(candidate):
        raise ResearchLocalizationError("localization does not replay exactly")
    return dict(candidate)


def select_localized(product: Mapping[str, Any], candidate: Mapping[str, Any] | None) -> dict[str, Any]:
    """Overlay display prose while retaining every authoritative product field."""

    if candidate is None:
        return copy.deepcopy(dict(product))
    valid = validate_localization(product, candidate)
    result = copy.deepcopy(dict(product))
    for source, translated in zip(result.get("sections") or [], valid["sections"]):
        source["title"] = translated["title"]
        source["body"] = translated["body"]
        source["gaps"] = list(translated["gaps"])
    result["localization"] = {
        "schema_version": valid["schema_version"], "target_locale": valid["target_locale"],
        "rules_version": valid["rules_version"], "source_content_hash": valid["source"]["content_hash"],
        "content_hash": valid["content_hash"],
    }
    return result


__all__ = ["ResearchLocalizationError", "SCHEMA_VERSION", "TARGET_LOCALE",
           "build_prompt", "build_verifier_prompt", "build_localization",
           "validate_localized_text", "validate_localization", "select_localized",
           "source_content_hash"]
