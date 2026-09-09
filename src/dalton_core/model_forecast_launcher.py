"""P13-M2: launch the driver-model child, one at a time.

The run is named by the company *and the thing it is about to model over* --
the specification, the filings behind the input table, and the generator that
turns one into the other. A tick that fires while none of those has moved is
the same ticket rather than a second child recomputing an identical model.

No model configuration here, unlike the specification lane: this child makes no
model call, so there is nothing to configure and nothing to gate on.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .lane_child_launcher import LaneChildLauncher, LaneChildRejected

TICKET_PREFIX = "model-forecast-run"


class ModelForecastLauncher(LaneChildLauncher):
    """Spawn ``dalton_core.company_model_forecast_cli`` against the writer's state."""

    TICKET_PREFIX = TICKET_PREFIX
    TICKETS_DIRNAME = "model-forecast-runs"
    CHILD_MODULE = "dalton_core.company_model_forecast_cli"

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

    def start(self, *, company_ref: str, model_digest: str) -> dict[str, Any]:
        if not isinstance(company_ref, str) or not company_ref.strip():
            raise LaneChildRejected("a driver model run needs a company")
        if not isinstance(model_digest, str) or len(model_digest) != 64:
            raise LaneChildRejected("model_digest must be a sha256 digest")
        company_ref = company_ref.strip()
        digest = hashlib.sha256(
            f"{self.TICKET_PREFIX}|{company_ref}|{model_digest}".encode("utf-8")
        ).hexdigest()[:24]
        return self.spawn(
            digest=digest,
            record={"company_ref": company_ref, "model_digest": model_digest},
            company_ref=company_ref,
        )


__all__ = ["TICKET_PREFIX", "ModelForecastLauncher"]
