"""Shared rule for the out-of-process lane children (P9d-14).

Every mission lane runs its work in a child the writer spawns and tracks
through an owner-only ``ticket.json`` next to the child's own ``summary.json``.

**A finished child was called orphaned.**  Settlement happens on the next
tick, up to five minutes after the child exits.  If the writer restarted in
that gap it lost the process handle, saw a ``running`` ticket with a dead pid,
and settled it ``orphaned``, discarding work that had completed: thirteen
tickets across three lanes on 2026-09-07, each parking a company/spec pair for
its retry interval.  :func:`adopt_finished_child` closes that gap.  When the
pid is gone and the child left its own final ``summary.json`` with a terminal
status, the ticket takes that status and says it did so
(``adopted_from_summary``).  This is not guessing success from a stray file:
the summary is the child's own record, written into a per-launch owner-only
directory, and every settle path still re-verifies authority (a fetch or
acquisition summary that says ``succeeded`` is only honoured if the bytes are in
Core through this source's own connector).  A dead pid with no summary, or a
summary without a terminal status, stays ``orphaned``.  The SEC lane launcher
is deliberately not changed: its test that a dead pid must never be promoted
from disk stands, because its child writes formal Claims itself.

A note on what this is *not* fixing.  Children that looked alive for minutes
after writing their summary were not hung; they had exited and were zombies
the writer had not yet reaped, because it only polls the handle on its next
tick.  ``kill(pid, 0)`` succeeds on a zombie, which is why the deploy drain
kept waiting on them.  That is handled in ``launch_drain``, not here.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

TERMINAL_SUMMARY_STATUSES = frozenset({"succeeded", "failed"})


def adopt_finished_child(record: dict[str, Any], summary_path: Path, *, now: str) -> bool:
    """Settle a ``running`` ticket whose pid is gone from the child's own summary.

    Mutates ``record`` in place and returns True when adopted; returns False
    (leaving ``record`` untouched) when there is no usable summary, in which
    case the caller settles ``orphaned``.
    """

    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(summary, Mapping):
        return False
    status = summary.get("status")
    if status not in TERMINAL_SUMMARY_STATUSES:
        return False
    record["status"] = status
    record["exit_code"] = 0 if status == "succeeded" else 1
    record["completed_at"] = now
    record["adopted_from_summary"] = True
    return True


__all__ = ["TERMINAL_SUMMARY_STATUSES", "adopt_finished_child"]
