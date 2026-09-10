"""C1: launch the calendar child, one at a time.

The run is named by the company and the day it is about, so a tick that fires
while a child is running does not start a second one, and a company already
asked about today is the same ticket rather than a second call against a source
that has not agreed to serve us. A calendar changes a handful of times a year;
asking twice in one day is spending a stranger's goodwill on nothing.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .lane_child_launcher import LaneChildLauncher, LaneChildRejected
from .connector_governance import ConnectorGovernance, YFINANCE_CALENDAR_KIND

TICKET_PREFIX = "catalyst-calendar-run"


class CatalystCalendarLauncher(LaneChildLauncher):
    """Spawn ``dalton_core.catalyst_calendar_cli`` against the writer's state."""

    TICKET_PREFIX = TICKET_PREFIX
    TICKETS_DIRNAME = "catalyst-calendar-runs"
    CHILD_MODULE = "dalton_core.catalyst_calendar_cli"

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
            raise LaneChildRejected("a calendar run needs an approved yfinance-calendar record")
        try:
            governance = ConnectorGovernance.load(self.governance_path)
        except Exception as exc:
            raise LaneChildRejected(f"invalid yfinance-calendar governance: {exc}") from exc
        if governance.kind != YFINANCE_CALENDAR_KIND:
            raise LaneChildRejected("the governance record covers a different capability")
        return governance

    def governance_identity(self) -> str:
        return self.load_governance().content_hash

    def _command(
        self, *, ticket_dir: Path, company_ref: str, ticker: str,
        issuer: str | None, as_of: str,
    ) -> list[str]:
        command = [
            self.python_executable, "-m", self.CHILD_MODULE,
            "--state-dir", str(self.state_dir),
            "--governance", str(self.governance_path),
            "--company-ref", company_ref,
            "--ticker", ticker,
            "--as-of", as_of,
            "--actor-ref", self.actor_ref,
            "--allow-network",
            "--summary-dir", str(ticket_dir), "--quiet",
        ]
        if issuer:
            command.extend(["--issuer", issuer])
        return command

    def start(
        self, *, company_ref: str, ticker: str, as_of: str,
        issuer: str | None = None,
    ) -> dict[str, Any]:
        governance = self.load_governance()
        if not governance.approved:
            raise LaneChildRejected("the yfinance-calendar governance record is not approved")
        for name, value in (
            ("company_ref", company_ref), ("ticker", ticker), ("as_of", as_of),
        ):
            if not isinstance(value, str) or not value.strip():
                raise LaneChildRejected(f"a calendar run needs a {name}")
        company_ref, ticker = company_ref.strip(), ticker.strip().upper()
        as_of = as_of.strip()
        issuer = None if issuer is None else str(issuer).strip() or None
        digest = hashlib.sha256(
            f"{self.TICKET_PREFIX}|{company_ref}|{ticker}|{issuer or ''}|{as_of}|{governance.content_hash}"
            .encode("utf-8")
        ).hexdigest()[:24]
        return self.spawn(
            digest=digest,
            record={
                "company_ref": company_ref, "ticker": ticker,
                "issuer": issuer, "as_of": as_of,
                "governance_configured": self.configured,
                "governance_ref": governance.id,
                "governance_hash": governance.content_hash,
            },
            company_ref=company_ref, ticker=ticker, issuer=issuer, as_of=as_of,
        )


__all__ = ["TICKET_PREFIX", "CatalystCalendarLauncher"]
