"""Conservative provider-side input token estimate for model prompts.

Dalton's frozen ContextPack tokenizer (``tokenizer:dalton-search-token:0.1``)
counts a run of CJK characters as one token.  Providers do not.  Three live
DeepSeek V4 Flash Agenda cycles measured the gap on the exact prompts stored
in the Scheduler:

    2026-08-24  17,537 chars  2,121 Dalton tokens   7,251 provider input tokens
    2026-08-25  20,300 chars  2,495 Dalton tokens   8,480 provider input tokens
    2026-08-26  22,119 chars  2,719 Dalton tokens   9,284 provider input tokens

That is 2.38-2.42 characters per provider token, while the Dalton count
understates the provider count 3.4x.  The policy's ``max_input_tokens`` is
enforced against provider telemetry *after* the paid call
(``OpenClawModelAdapter._assert_budget``), so any pre-flight budget decision
has to be measured in the provider's unit, not the Dalton one.

``CHARS_PER_PROVIDER_TOKEN = 2.2`` keeps roughly an 8% margin under every
observed ratio.  The estimate is deliberately simple and deterministic; it is
recorded next to each use under ``PROVIDER_INPUT_ESTIMATOR_REF`` so a later
recalibration is a new ref, not a silent change.
"""
from __future__ import annotations

import math

PROVIDER_INPUT_ESTIMATOR_REF = "estimator:provider-input-chars-per-token:2.2"
CHARS_PER_PROVIDER_TOKEN = 2.2

# The three live observations above, kept as a regression anchor: the
# estimator must never fall below any of them.
LIVE_CALIBRATION_OBSERVATIONS: tuple[tuple[int, int], ...] = (
    (17_537, 7_251),
    (20_300, 8_480),
    (22_119, 9_284),
)


def estimate_provider_input_tokens(text: str) -> int:
    """Return a conservative provider input-token estimate for ``text``."""

    if not isinstance(text, str):
        raise TypeError("provider token estimate requires text")
    return math.ceil(len(text) / CHARS_PER_PROVIDER_TOKEN)


__all__ = [
    "CHARS_PER_PROVIDER_TOKEN",
    "LIVE_CALIBRATION_OBSERVATIONS",
    "PROVIDER_INPUT_ESTIMATOR_REF",
    "estimate_provider_input_tokens",
]
