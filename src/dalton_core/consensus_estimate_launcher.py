"""P11b: launch the consensus child, one at a time.

The run is named by the company, the ticker and the day, so a tick that fires
while a child is running does not start a second one for the same question, and
a second ask on the same afternoon is the same ticket rather than a second call
against a source that never agreed to serve us. The day rather than the
timestamp: the answer to "what does the street expect" moves in weeks, and the
version chain says ``duplicate`` when it has not moved at all.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .lane_child_launcher import LaneChildLauncher, LaneChildRejected
from .connector_governance import ConnectorGovernance, YFINANCE_ANALYST_ESTIMATES_KIND

TICKET_PREFIX = "consensus-estimate-run"


class ConsensusEstimateLauncher(LaneChildLauncher):
    """Spawn ``dalton_core.consensus_estimate_cli`` against the writer's state."""

    TICKET_PREFIX = TICKET_PREFIX
    TICKETS_DIRNAME = "consensus-estimate-runs"
    CHILD_MODULE = "dalton_core.consensus_estimate_cli"

    def __init__(
        self,
        *,
        state_dir: str | Path,
        governance_path: str | Path | None = None,
        actor_ref: str = "automation:coverage-mission",
        **kwargs: Any,
    ) -> None:
        super().__init__(state_dir=state_dir, **kwargs)
        self.governance_path = (
            None if governance_path is None
            else Path(governance_path).expanduser().resolve()
        )
        self.actor_ref = actor_ref

    @property
    def configured(self) -> bool:
        """Whether an approval is wired. Without one the child refuses."""

        return self.governance_path is not None

    def load_governance(self):
        if not self.configured:
            raise LaneChildRejected("a consensus run needs an approved yfinance analyst-estimates record")
        try:
            governance = ConnectorGovernance.load(self.governance_path)
        except Exception as exc:
            raise LaneChildRejected(f"invalid yfinance analyst-estimates governance: {exc}") from exc
        if governance.kind != YFINANCE_ANALYST_ESTIMATES_KIND:
            raise LaneChildRejected("the governance record covers a different capability")
        return governance

    def governance_identity(self) -> str:
        return self.load_governance().content_hash

    def _command(
        self, *, ticket_dir: Path, company_ref: str, ticker: str,
        fiscal_year_end: str, last_reported_period_end: str,
    ) -> list[str]:
        return [
            self.python_executable, "-m", self.CHILD_MODULE,
            "--state-dir", str(self.state_dir),
            "--governance", str(self.governance_path),
            "--company-ref", company_ref,
            "--ticker", ticker,
            "--fiscal-year-end", fiscal_year_end,
            "--last-reported-period-end", last_reported_period_end,
            "--actor-ref", self.actor_ref,
            "--allow-network",
            "--summary-dir", str(ticket_dir), "--quiet",
        ]

    def start(
        self, *, company_ref: str, ticker: str, fiscal_year_end: str,
        last_reported_period_end: str, day: str,
    ) -> dict[str, Any]:
        governance = self.load_governance()
        if not governance.approved:
            raise LaneChildRejected("the yfinance analyst-estimates governance record is not approved")
        for name, value in (
            ("company_ref", company_ref), ("ticker", ticker),
            ("fiscal_year_end", fiscal_year_end),
            ("last_reported_period_end", last_reported_period_end),
            ("day", day),
        ):
            if not isinstance(value, str) or not value.strip():
                raise LaneChildRejected(f"a consensus run needs a {name}")
        company_ref, ticker = company_ref.strip(), ticker.strip().upper()
        fiscal_year_end = fiscal_year_end.strip()
        last_reported_period_end = last_reported_period_end.strip()
        digest = hashlib.sha256(
            f"{self.TICKET_PREFIX}|{company_ref}|{ticker}|{fiscal_year_end}|{last_reported_period_end}|{day.strip()}|{governance.content_hash}".encode("utf-8")
        ).hexdigest()[:24]
        return self.spawn(
            digest=digest,
            record={
                "company_ref": company_ref, "ticker": ticker,
                "fiscal_year_end": fiscal_year_end,
                "last_reported_period_end": last_reported_period_end,
                "day": day.strip(),
                "governance_configured": self.configured,
                "governance_ref": governance.id,
                "governance_hash": governance.content_hash,
            },
            company_ref=company_ref, ticker=ticker,
            fiscal_year_end=fiscal_year_end,
            last_reported_period_end=last_reported_period_end,
        )


__all__ = ["TICKET_PREFIX", "ConsensusEstimateLauncher"]
