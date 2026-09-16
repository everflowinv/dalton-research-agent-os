"""P11c-E: launch the valuation-snapshot child, one at a time.

The run is named by the company and by the fingerprint of what it would be a
snapshot *of* -- the price version, the share observation, the filed role
totals and the coverage note, which is exactly what
``valuation_snapshot_cli.snapshot_fingerprint`` hashes. A tick that fires
while none of those has moved names the same ticket rather than spawning a
second child to recompute an identical snapshot the authority would refuse as
a duplicate anyway.

No model configuration and no connector governance, like the sensitivity
lane's launcher: this child makes no model call and reaches no network, so
there is nothing to point it at and nothing an owner has to approve first.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .lane_child_launcher import LaneChildLauncher, LaneChildRejected

TICKET_PREFIX = "valuation-snapshot-run"


class ValuationSnapshotLauncher(LaneChildLauncher):
    """Spawn ``dalton_core.valuation_snapshot_cli`` against the writer's state."""

    TICKET_PREFIX = TICKET_PREFIX
    TICKETS_DIRNAME = "valuation-snapshot-runs"
    CHILD_MODULE = "dalton_core.valuation_snapshot_cli"

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

    def start(self, *, company_ref: str, fingerprint: str) -> dict[str, Any]:
        if not isinstance(company_ref, str) or not company_ref.strip():
            raise LaneChildRejected("a valuation snapshot run needs a company")
        if not isinstance(fingerprint, str) or len(fingerprint) != 64:
            raise LaneChildRejected("fingerprint must be a sha256 digest")
        company_ref = company_ref.strip()
        digest = hashlib.sha256(
            f"{self.TICKET_PREFIX}|{company_ref}|{fingerprint}".encode("utf-8")
        ).hexdigest()[:24]
        return self.spawn(
            digest=digest,
            record={"company_ref": company_ref, "fingerprint": fingerprint},
            company_ref=company_ref,
        )


__all__ = ["TICKET_PREFIX", "ValuationSnapshotLauncher"]
