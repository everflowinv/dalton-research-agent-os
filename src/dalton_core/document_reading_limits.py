"""Configured source reading bounds, separate from paid invocation budgets."""
from collections.abc import Mapping

DEFAULT_READING_LIMITS = {
    "window_chars": 12000,
    "quote_chars": 1200,
    "max_document_chars": 600000,
    "max_pdf_pages": 400,
    "max_decompressed_bytes": 8_000_000,
}


def resolve_reading_limits(config=None):
    """Keep legacy geometry unless explicitly configured; never truncate silently."""
    if config is None:
        config = {}
    if not isinstance(config, Mapping):
        raise ValueError("reading configuration must be an object")
    overrides = config.get("reading_limits", {})
    if not isinstance(overrides, Mapping) or set(overrides) - set(DEFAULT_READING_LIMITS):
        raise ValueError("reading_limits has an invalid closed shape")
    limits = {**DEFAULT_READING_LIMITS, **overrides}
    for field, value in limits.items():
        if type(value) is not int or value <= 0:
            raise ValueError(f"reading_limits.{field} must be a positive integer")
    if limits["quote_chars"] > limits["window_chars"]:
        raise ValueError("reading_limits.quote_chars must not exceed window_chars")
    if limits["window_chars"] > limits["max_document_chars"]:
        raise ValueError("reading_limits.window_chars must not exceed max_document_chars")
    return limits
