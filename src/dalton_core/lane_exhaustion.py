"""Truthful resting status when every candidate was skipped by a lane hold."""

from __future__ import annotations

from collections import Counter
from typing import Any, Mapping, Sequence

FAILURE_SKIP_REASONS = frozenset({"held", "parked", "terminal", "not_permitted"})


def exhausted_by_failures(
    skipped: Sequence[Mapping[str, Any]], *, success_reason: str,
) -> dict[str, Any]:
    """Return idle only when no durable failure prevented candidate work."""

    failures = [str(item.get("reason")) for item in skipped
                if item.get("reason") in FAILURE_SKIP_REASONS]
    if not failures:
        return {"status": "idle", "reason": success_reason}
    counts = Counter(failures)
    detail = ", ".join(f"{name}={counts[name]}" for name in sorted(counts))
    return {
        "status": "held",
        "reason": f"covered-company refresh is blocked ({detail})",
        "blocked_count": len(failures),
        "failure_counts": dict(sorted(counts.items())),
    }


__all__ = ["FAILURE_SKIP_REASONS", "exhausted_by_failures"]
