"""ADR-0014: the cockpit's 「回报归因」 company-card panel.

Read-only over the Core; degrades to nothing on a Core that never ran the
return attribution lane.  Nothing here writes: the objects are derived, and
the owner's lever over them is the lane pause, like any other lane.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from typing import Any

from .return_attribution import company_view, table_exists


def card_views(core: sqlite3.Connection, company_refs: Sequence[str],
               industry_ref: str | None = None) -> dict[str, dict[str, Any]]:
    """company_ref -> the panel; {} on a Core without the tables."""

    if not (table_exists(core, "income_bridge_versions")
            or table_exists(core, "holding_period_return_bridges")):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for company_ref in company_refs:
        try:
            view = company_view(core, company_ref, industry_ref)
        except Exception:  # noqa: BLE001 - one company's panel never breaks the page
            continue
        if view is not None:
            out[company_ref] = view
    return out


__all__ = ["card_views"]
