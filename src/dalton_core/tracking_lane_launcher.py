"""P14a: launch the tracking child, one at a time.

The run is named by the mission version and the hour it belongs to.  Not by a
company, unlike every other lane's launcher, because this child does not pick
one: tracking is resident and every tracked company is scanned on every run.
Not by the minute either -- a tick every five minutes would otherwise name a
new ticket every five minutes for a scan whose answer changes at the pace
documents arrive.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .lane_child_launcher import LaneChildLauncher, LaneChildRejected

TICKET_PREFIX = "tracking-run"


class TrackingLaneLauncher(LaneChildLauncher):
    """Spawn ``dalton_core.tracking_lane_cli`` against the writer's state."""

    TICKET_PREFIX = TICKET_PREFIX
    TICKETS_DIRNAME = "tracking-runs"
    CHILD_MODULE = "dalton_core.tracking_lane_cli"

    def __init__(self, *, state_dir: str | Path, policy_path: str | Path, **kwargs: Any) -> None:
        super().__init__(state_dir=state_dir, **kwargs)
        self.policy_path = Path(policy_path).expanduser().resolve()

    @property
    def configured(self) -> bool:
        """The policy file is the switch: no baselines, no tracking."""

        return self.policy_path.is_file()

    def _command(self, *, ticket_dir: Path) -> list[str]:
        return [
            self.python_executable, "-m", self.CHILD_MODULE,
            "--state-dir", str(self.state_dir),
            "--tracking-policy", str(self.policy_path),
            "--summary-dir", str(ticket_dir), "--quiet",
        ]

    def start(self, *, window_ref: str) -> dict[str, Any]:
        if not isinstance(window_ref, str) or not window_ref.strip():
            raise LaneChildRejected("a tracking run needs a window ref")
        if not self.configured:
            raise LaneChildRejected(
                f"no tracking policy at {self.policy_path}; the lane is not installed"
            )
        digest = hashlib.sha256(
            f"{self.TICKET_PREFIX}|{window_ref.strip()}".encode("utf-8")
        ).hexdigest()[:24]
        return self.spawn(digest=digest, record={"window_ref": window_ref.strip()})


__all__ = ["TICKET_PREFIX", "TrackingLaneLauncher"]
