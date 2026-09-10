"""P13-M3: launch the sensitivity child, one at a time.

The run is named by the company and by the pair of things the table is *of* --
the driver model version and the street estimate behind it, which is exactly
what ``forecast_sensitivity.fingerprint`` hashes. A tick that fires while
neither has moved names the same ticket rather than spawning a second child to
recompute an identical table.

No model configuration, like the driver-model lane: this child makes no model
call, so there is nothing to configure and nothing to gate on.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .lane_child_launcher import LaneChildLauncher, LaneChildRejected

TICKET_PREFIX = "forecast-sensitivity-run"


class ForecastSensitivityLauncher(LaneChildLauncher):
    """Spawn ``dalton_core.forecast_sensitivity_cli`` against the writer's state."""

    TICKET_PREFIX = TICKET_PREFIX
    TICKETS_DIRNAME = "forecast-sensitivity-runs"
    CHILD_MODULE = "dalton_core.forecast_sensitivity_cli"

    def __init__(self, *, state_dir: str | Path, **kwargs: Any) -> None:
        super().__init__(state_dir=state_dir, **kwargs)

    @property
    def configured(self) -> bool:
        """Always. The child computes; there is nothing it could be missing."""

        return True

    def _command(self, *, ticket_dir: Path, company_ref: str) -> list[str]:
        return [
            self.python_executable, "-m", self.CHILD_MODULE,
            "--state-dir", str(self.state_dir),
            "--company-ref", company_ref,
            "--summary-dir", str(ticket_dir), "--quiet",
        ]

    def start(self, *, company_ref: str, projection_digest: str) -> dict[str, Any]:
        if not isinstance(company_ref, str) or not company_ref.strip():
            raise LaneChildRejected("a sensitivity run needs a company")
        if not isinstance(projection_digest, str) or len(projection_digest) != 64:
            raise LaneChildRejected("projection_digest must be a sha256 digest")
        company_ref = company_ref.strip()
        digest = hashlib.sha256(
            f"{self.TICKET_PREFIX}|{company_ref}|{projection_digest}".encode("utf-8")
        ).hexdigest()[:24]
        return self.spawn(
            digest=digest,
            record={"company_ref": company_ref,
                    "projection_digest": projection_digest},
            company_ref=company_ref,
        )


__all__ = ["TICKET_PREFIX", "ForecastSensitivityLauncher"]
