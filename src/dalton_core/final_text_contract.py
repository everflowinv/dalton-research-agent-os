"""Shared language rules for research prose that a person reads."""

from __future__ import annotations

FINAL_TEXT_RULES_VERSION = "simplified-chinese-research-prose:0.3"


def final_text_instructions() -> tuple[str, ...]:
    """Return the common prose contract used by every research drafter."""

    return (
        "Human-facing prose fields must use fluent Simplified Chinese. Keep real company names, "
        "tickers, public model names and verbatim source quotations in their source language when "
        "translating them would reduce precision. Internal system vocabulary is not a proper noun.",
        "In normal prose use these Chinese terms: Claim/Claims→已核实结论, thesis→投资论点, "
        "ThesisRevisionCandidate→论点修订建议, debate→争议分析, dossier→深度研究档案, "
        "lane→研究环节. Translate equivalent inflections and plurals too. Keep raw enum keys, "
        "record IDs and hashes only in marked technical details, never in the reader-facing body.",
        "Describe the actual research meaning directly. Avoid implementation-defensive wording such "
        "as '不改动任何权威', '不会写入权威', '仅作展示层处理', '由 automation 处理' or "
        "'受 pipeline/constitution/mandate 约束'. State the real evidence, limitation, decision or "
        "next step instead, without weakening a genuine permission or approval boundary.",
        "Lead with the clearest evidence-supported judgement, then give its decisive evidence "
        "and consequence. Put only uncertainties that could change that judgement afterward.",
        "Combine overlapping caveats. Do not repeat defensive pairs such as '这不证明……、不能"
        "据此……、不作为……' when one precise limitation says the same thing. Do not pad every "
        "paragraph with both sides after the preferred case is clear.",
        "Preserve the meaning of genuine gaps, source boundaries, numerical limits, approval "
        "boundaries and refusals. Never invent a fact, source, number, approval or completed action "
        "to make the Chinese more decisive. Keep authoritative numeric values unchanged; for "
        "display, convert large USD amounts to readable 万美元/亿美元 units, show percentages "
        "to one decimal place, and keep EPS or ARPU as separately labelled small-value measures.",
    )


__all__ = ["FINAL_TEXT_RULES_VERSION", "final_text_instructions"]
