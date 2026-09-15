"""Shared language rules for research prose that a person reads."""

from __future__ import annotations

FINAL_TEXT_RULES_VERSION = "simplified-chinese-research-prose:0.4"


def final_text_instructions() -> tuple[str, ...]:
    """Return the common prose contract used by every research drafter."""

    return (
        "Human-facing prose fields must use fluent Simplified Chinese. Keep real company names, "
        "tickers, public model names and verbatim source quotations in their source language when "
        "translating them would reduce precision. Internal system vocabulary is not a proper noun.",
        "A normalized_statement, generated evidence sentence, structured summary or metric label "
        "is system-authored prose, not a verbatim source quotation. Translate it into fluent Chinese "
        "and never label it as 'verbatim', 'original English' or equivalent unless the source "
        "structure explicitly marks a direct quotation. Preserve genuinely marked quotations.",
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
        "The reader sees only the current version. Never write about versions in the body: no "
        "prior-version phrasing (上一·版 / 前一版 / 本次更新 / 相比之前 / 新版、旧版) or "
        "equivalent. Judge prior "
        "material silently -- carry forward what still holds, revise what changed -- and state "
        "today's view as the view, full stop. Process notes about revisions belong in the "
        "system's review workflow, never in the deliverable.",
        "Paragraph the prose for reading. Each section body is split into paragraphs of two to "
        "five sentences, one theme per paragraph with a blank line between them; a single "
        "unbroken block of prose is a defect. Do not number paragraphs or write ordered lists.",
        "State what is, not what is not. Defensive negations -- '不是', '不能', '不代表', "
        "'不等于', '并非', '而非', '无法', '不再', '不必', '并不' -- are banned from final "
        "prose unless the negation itself carries the research meaning (a genuine contradiction, "
        "a hard refusal, or a boundary the reader must know). Rewrite around the positive "
        "statement: '增速不代表改善' → '增速放缓，成分仍以价格贡献为主'.",
        "Preserve the meaning of genuine gaps, source boundaries, numerical limits, approval "
        "boundaries and refusals. Never invent a fact, source, number, approval or completed action "
        "to make the Chinese more decisive. Keep authoritative numeric values unchanged; for "
        "display, convert large USD amounts to readable 万美元/亿美元 units, show percentages "
        "to one decimal place, and keep EPS or ARPU as separately labelled small-value measures.",
    )


__all__ = ["FINAL_TEXT_RULES_VERSION", "final_text_instructions"]
