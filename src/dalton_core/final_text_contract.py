"""Shared language rules for research prose that a person reads."""

from __future__ import annotations

FINAL_TEXT_RULES_VERSION = "simplified-chinese-research-prose:0.1"


def final_text_instructions() -> tuple[str, ...]:
    """Return the common prose contract used by every research drafter."""

    return (
        "Human-facing prose fields must use fluent Simplified Chinese. Keep company names, "
        "tickers, established technical terms and verbatim quotations in their source language "
        "when translating them would reduce precision.",
        "Lead with the clearest evidence-supported judgement, then give its decisive evidence "
        "and consequence. Put only uncertainties that could change that judgement afterward.",
        "Combine overlapping caveats. Do not repeat defensive pairs such as '这不证明……、不能"
        "据此……、不作为……' when one precise limitation says the same thing. Do not pad every "
        "paragraph with both sides after the preferred case is clear.",
        "Preserve the meaning of genuine gaps, source boundaries, numerical limits, approval "
        "boundaries and refusals. Never invent a fact, source, number, approval or completed action "
        "to make the Chinese more decisive.",
    )


__all__ = ["FINAL_TEXT_RULES_VERSION", "final_text_instructions"]
