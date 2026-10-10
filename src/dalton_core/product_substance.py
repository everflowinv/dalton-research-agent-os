"""Batch ap 1-3: substance completeness and the publication gate.

``required_sections_present`` used to ask only whether a title existed and
whether an empty body at least carried a gap -- so IBM's empty S6 passed, and
a section whose whole body was 「现有材料缺少……」 counted as written.  Here a
section is judged on its substance:

* ``empty``         -- no body and no gap;
* ``heading_only``  -- the body is a heading or its own title, no sentence;
* ``gap_only``      -- once gap sentences are set aside, nothing is left;
* ``placeholder_only`` -- the body is an indirect placeholder;
* ``substantive``   -- anything else.

A product with a failed section cannot be marked complete, published or
localised (:func:`publication_blockers`).  Two companion checks: every figure
entry carries metric / period / unit / source, and every figure in prose is
shown at magnitude-appropriate rounding (``numeric_display``).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from .product_contract import MATERIAL_MISSING_RE, placeholder_findings, sentences

SUBSTANCE_VERSION = "product-substance:1"
SECTION_STATUSES: tuple[str, ...] = (
    "substantive", "empty", "heading_only", "gap_only", "placeholder_only",
)
FAILED_STATUSES = frozenset(SECTION_STATUSES) - {"substantive"}
#: Characters of non-gap prose a section needs to count as written.
MIN_SUBSTANCE_CHARS = 30

#: A sentence that only says something is missing.
GAP_SENTENCE_RE = re.compile(
    r"^(?:[^。；]{0,30})(?:" + MATERIAL_MISSING_RE.pattern + r"|暂无|尚无|没有(?:可用|足够)|"
    r"无法(?:判断|给出|写出|评估)|待补|待验证|未能取得|未披露|不披露|缺少|缺乏|还缺|仍缺|"
    r"按[^。；]{0,12}纪律留空|留空|本节暂缺|这一节(?:暂缺|已丢弃))")


def _strip(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


#: A section whose job *is* to list gaps (「附录：我们不知道什么」, a brief's
#: 「证据缺口」) is judged on having a body, not on what the body is about.
GAP_SECTION_TITLE_RE = re.compile(r"我们不知道|证据缺口|缺口清单|未知项|unknowns?|open questions", re.IGNORECASE)


def section_substance(section: Mapping[str, Any], *, min_chars: int = MIN_SUBSTANCE_CHARS) -> dict[str, Any]:
    """``{"status", "chars", "reason"}`` for one section."""

    title = str(section.get("title") or "")
    body = str(section.get("body") or "").strip()
    gaps = [str(gap) for gap in (section.get("gaps") or []) if str(gap).strip()]
    if not body:
        if gaps:
            return {"status": "gap_only", "chars": 0, "reason": "正文为空，只有缺口说明"}
        return {"status": "empty", "chars": 0, "reason": "既没有正文也没有缺口说明"}
    if GAP_SECTION_TITLE_RE.search(title):
        return {"status": "substantive", "chars": len(_strip(body)), "reason": ""}
    flat = _strip(body)
    if flat == _strip(title) or (len(flat) <= 30 and not re.search(r"[。！？；.!?]", body)
                                 and "\n" not in body.strip()):
        return {"status": "heading_only", "chars": len(flat), "reason": "正文只有标题或一行标签"}
    if placeholder_findings(body) and len(_strip(_remove_placeholders(body))) < min_chars:
        return {"status": "placeholder_only", "chars": len(flat), "reason": "正文只有间接占位符"}
    all_sentences = sentences(body)
    substantive = [s for s in all_sentences if not GAP_SENTENCE_RE.search(s)]
    chars = len(_strip("".join(substantive)))
    # Only a body that *says* something is missing is judged on what is left
    # once those sentences are set aside; a short plain sentence is short, not
    # a gap note.
    if len(substantive) < len(all_sentences) and chars < min_chars:
        return {"status": "gap_only", "chars": chars,
                "reason": f"除缺口句外只剩 {chars} 字实质内容"}
    return {"status": "substantive", "chars": chars, "reason": ""}


def _remove_placeholders(body: str) -> str:
    out = body
    for finding in placeholder_findings(body):
        out = out.replace(finding["matched"], "")
    return out


#: A gap note that names how the question will be answered: a MaterialRequest
#: or the next verification step.  Only products whose contract admits an
#: unanswered item (the Deep Insight Gate's 12 questions) accept it.
UNANSWERED_GAP_RE = re.compile(r"下一步|material-request:|MR-[A-Za-z0-9]|evidence_that_would_answer")


def is_honest_unanswered(section: Mapping[str, Any]) -> bool:
    """No body, and a gap that says what would answer it."""

    return (not str(section.get("body") or "").strip()
            and any(UNANSWERED_GAP_RE.search(str(gap)) for gap in section.get("gaps") or ()))


def required_sections_findings(
    sections: Sequence[Mapping[str, Any]], expected: Sequence[str] | None,
    *, allow_unanswered: bool = False,
) -> list[dict[str, Any]]:
    """Missing titles, and sections that fail on substance.

    ``allow_unanswered`` is for a product whose contract makes "unanswered,
    with the request or next step that would answer it" a legal answer (the
    gate's ``unknown`` + MaterialRequest); everywhere else a gap-only section
    fails (ap 1).
    """

    findings: list[dict[str, Any]] = []
    written = [str(section.get("title") or "") for section in sections]
    for title in expected or ():
        if title not in written:
            findings.append({"code": "missing_section", "title": title})
    for section in sections:
        if allow_unanswered and is_honest_unanswered(section):
            continue
        verdict = section_substance(section)
        if verdict["status"] in FAILED_STATUSES:
            findings.append({"code": verdict["status"], "title": str(section.get("title") or ""),
                             "reason": verdict["reason"]})
    return findings


def failed_sections(sections: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"title": str(section.get("title") or ""), **section_substance(section)}
        for section in sections if section_substance(section)["status"] in FAILED_STATUSES
    ]


def publication_blockers(sections: Sequence[Mapping[str, Any]], *,
                         expected: Sequence[str] | None = None) -> list[dict[str, Any]]:
    """Why a product may not be marked complete, published or localised."""

    blockers = required_sections_findings(sections, expected)
    for section in sections:
        for finding in placeholder_findings(str(section.get("body") or "")):
            blockers.append({"code": finding["code"], "title": str(section.get("title") or ""),
                             "matched": finding["matched"]})
    return blockers


# ---------------------------------------------------------------------------
# figures: metadata and display rounding
# ---------------------------------------------------------------------------

_SOURCE_KEYS = ("claim_version_ref", "cell", "passage_ref", "derivation", "number_ref", "estimate")


def number_metadata_findings(sections: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Figure entries missing metric, period, unit or source."""

    findings = []
    for section in sections:
        for item in section.get("numbers") or ():
            missing = []
            if not any(item.get(key) for key in _SOURCE_KEYS):
                missing.append("source")
            if not item.get("period") and not item.get("derivation"):
                missing.append("period")
            if "metric" in item or "unit" in item or section.get("_strict_numbers"):
                if not item.get("metric"):
                    missing.append("metric")
                if not item.get("unit"):
                    missing.append("unit")
            if missing:
                findings.append({"code": "number_metadata_missing",
                                 "section": str(section.get("title") or ""),
                                 "text": str(item.get("text") or "")[:80], "missing": missing})
    return findings


_DISPLAY_TOKEN_RE = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?(?![\w.])")


def display_rounding_findings(body: str) -> list[dict[str, Any]]:
    """Machine-precision figures a reader should not see (18718000000, 0.123456)."""

    from .numeric_display import humanize_machine_numbers

    findings = []
    for match in _DISPLAY_TOKEN_RE.finditer(body or ""):
        token = match.group(0)
        digits = token.lstrip("-").replace(",", "")
        whole, _, frac = digits.partition(".")
        if (len(whole) >= 7 and not re.fullmatch(r"(?:19|20)\d{6}", whole)) or len(frac) >= 3:
            shown = humanize_machine_numbers(token)
            if shown != token:
                findings.append({"code": "machine_precision_number", "figure": token,
                                 "display": shown})
    return findings


__all__ = [
    "FAILED_STATUSES", "GAP_SECTION_TITLE_RE", "GAP_SENTENCE_RE", "MIN_SUBSTANCE_CHARS", "SECTION_STATUSES",
    "SUBSTANCE_VERSION", "UNANSWERED_GAP_RE", "display_rounding_findings", "failed_sections",
    "is_honest_unanswered",
    "number_metadata_findings", "publication_blockers", "required_sections_findings",
    "section_substance",
]
