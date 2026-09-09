"""P14e: the child that admits research tasks, one run at a time.

Admission is two authority writes per inquiry -- a backlog question and a loop
version -- against a mission, a mandate, a plan and the probe catalogue.  It is
not slow, but it is a multi-table write against Core, and the four lanes that
already do that shape of work do it out of process for the same reason: a
writer request thread is abandoned after 30 seconds, and a lane that occupies
one is a lane the controller reports as dark.

Everything about the ticket machinery is ``LaneChildLauncher``'s (P13aj): one
child at a time, owner-only tickets, and a restart under a running child that
settles as *orphaned* rather than guessing success from a stray summary file.
What is left here is this lane's own: its prefix, its directory and its
command.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .lane_child_launcher import LaneChildLauncher, LaneChildRejected
from .store import canonical_json

TICKET_PREFIX = "research-task"
TICKETS_DIRNAME = "research-tasks"


class ResearchTaskLauncher(LaneChildLauncher):
    """Run one ad-hoc research admission pass."""

    TICKET_PREFIX = TICKET_PREFIX
    TICKETS_DIRNAME = TICKETS_DIRNAME
    CHILD_MODULE = "dalton_core.research_task_cli"

    def __init__(self, *, max_admissions_per_tick: int = 1, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        if (
            isinstance(max_admissions_per_tick, bool)
            or not isinstance(max_admissions_per_tick, int)
            or max_admissions_per_tick < 1
        ):
            raise LaneChildRejected("max_admissions_per_tick must be a positive integer")
        self.max_admissions_per_tick = max_admissions_per_tick

    def _command(self, *, ticket_dir: Path, **kwargs: Any) -> list[str]:
        return [
            self.python_executable, "-m", self.CHILD_MODULE,
            "--state-dir", str(self.state_dir),
            "--summary-dir", str(ticket_dir),
            "--max-admissions", str(self.max_admissions_per_tick),
            "--quiet",
        ]

    def start(self, *, plan_ref: str, signature: str) -> dict[str, Any]:
        """Start one admission pass for one plan.

        The digest is the plan and the signature, so re-running against an
        unchanged plan is the same ticket rather than a new directory every
        five minutes.
        """

        digest = hashlib.sha256(
            canonical_json({"plan_ref": plan_ref, "signature": signature}).encode("utf-8")
        ).hexdigest()[:24]
        return self.spawn(
            digest=digest,
            record={"plan_ref": plan_ref, "signature": signature},
        )


__all__ = ["TICKETS_DIRNAME", "TICKET_PREFIX", "ResearchTaskLauncher"]
