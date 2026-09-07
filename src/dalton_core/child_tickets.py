"""Shared rules for the out-of-process lane children (P9d-14 hardening).

Every mission lane runs its work in a child the writer spawns and tracks
through an owner-only ``ticket.json`` next to the child's own ``summary.json``.
Two things went wrong with that on 2026-09-07 and both are fixed here.

**A finished child was called orphaned.**  Settlement happens on the next
tick, up to five minutes after the child exits.  If the writer restarted in
that gap it lost the process handle, saw a ``running`` ticket with a dead pid,
and settled it ``orphaned``, discarding work that had completed: thirteen
tickets across three lanes in one day, each parking a company/spec pair for its
retry interval.  :func:`adopt_finished_child` closes that gap.  When the pid
is gone and the child left its own final ``summary.json`` with a terminal
status, the ticket takes that status and says it did so
(``adopted_from_summary``).  This is not guessing success from a stray file:
the summary is the child's own record, written into a per-launch owner-only
directory, and every settle path still re-verifies authority (a fetch or
acquisition summary that says ``succeeded`` is only honoured if the bytes are in
Core through this source's own connector).  A dead pid with no summary, or a
summary without a terminal status, stays ``orphaned``.  The SEC lane launcher
is deliberately not changed: its test that a dead pid must never be promoted
from disk stands, because its child writes formal Claims itself.

**A failed child did not exit.**  Twice a fetch child raised an unexpected
exception, wrote its summary and traceback, and then stayed alive until the
next writer restart, holding the lane's single acquisition slot the whole time.
The cause could not be reproduced outside the live host.  :func:`run_child`
removes the dependence on a clean interpreter shutdown: once ``main`` has
returned or raised, and stdio is flushed, the process exits through
``os._exit``.  The child has already closed its stores and written its summary
by then; nothing is lost.  Tests call ``main`` directly and never go through
this.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from collections.abc import Callable, Mapping
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


def run_child(main: Callable[[], int]) -> None:  # pragma: no cover - process boundary
    """Entry point for a lane child: run ``main`` and always exit."""

    code = 1
    try:
        code = int(main())
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    except BaseException:
        traceback.print_exc()
        code = 1
    finally:
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except Exception:
                pass
        os._exit(code)


__all__ = ["TERMINAL_SUMMARY_STATUSES", "adopt_finished_child", "run_child"]
