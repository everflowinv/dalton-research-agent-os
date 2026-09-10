"""P11a: launch the price child, one at a time.

The child fetches one company's window and publishes it; this decides when one
runs and keeps the ticket.

The run is named by the company *and the window it is about to ask for*, so a
tick that fires while a child is running does not start a second one for the
same question, and a window already fetched today is the same ticket rather
than a second call against a source that has not agreed to serve us.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .lane_child_launcher import LaneChildLauncher, LaneChildRejected
from .connector_governance import ConnectorGovernance

TICKET_PREFIX = "market-price-run"


class MarketPriceLauncher(LaneChildLauncher):
    """Spawn ``dalton_core.market_price_cli`` against the writer's state."""

    TICKET_PREFIX = TICKET_PREFIX
    TICKETS_DIRNAME = "market-price-runs"
    CHILD_MODULE = "dalton_core.market_price_cli"

    def __init__(
        self,
        *,
        state_dir: str | Path,
        governance_path: str | Path | None = None,
        proxy_config_path: str | Path | None = None,
        actor_ref: str = "automation:coverage-mission",
        **kwargs: Any,
    ) -> None:
        super().__init__(state_dir=state_dir, **kwargs)
        self.governance_path = (
            None if governance_path is None
            else Path(governance_path).expanduser().resolve()
        )
        self.actor_ref = actor_ref
        self.proxy_config_path = (None if proxy_config_path is None else
                                  Path(proxy_config_path).expanduser().resolve())

    @property
    def configured(self) -> bool:
        """Whether an approval is wired. Without one the child refuses."""

        return self.governance_path is not None

    def load_governance(self):
        if not self.configured:
            raise LaneChildRejected("a price run needs an approved yfinance daily-prices record")
        try:
            from .market_price_cli import _load_governance
            governance = _load_governance(self.governance_path)
        except Exception as exc:
            raise LaneChildRejected(f"invalid yfinance daily-prices governance: {exc}") from exc
        return governance

    def governance_identity(self) -> str:
        if not self.configured:
            return "unconfigured"
        return ConnectorGovernance.load(self.governance_path).content_hash

    def _command(
        self, *, ticket_dir: Path, company_ref: str, ticker: str,
        start: str, end: str,
    ) -> list[str]:
        return [
            self.python_executable, "-m", self.CHILD_MODULE,
            "--state-dir", str(self.state_dir),
            "--governance", str(self.governance_path),
            "--company-ref", company_ref,
            "--ticker", ticker,
            "--start", start,
            "--end", end,
            "--actor-ref", self.actor_ref,
            "--allow-network",
            "--summary-dir", str(ticket_dir), "--quiet",
        ]

    def start(
        self, *, company_ref: str, ticker: str, start: str, end: str,
    ) -> dict[str, Any]:
        governance = self.load_governance()
        for name, value in (
            ("company_ref", company_ref), ("ticker", ticker),
            ("start", start), ("end", end),
        ):
            if not isinstance(value, str) or not value.strip():
                raise LaneChildRejected(f"a price run needs a {name}")
        company_ref, ticker = company_ref.strip(), ticker.strip().upper()
        start, end = start.strip(), end.strip()
        if end < start:
            raise LaneChildRejected("a price window cannot end before it starts")
        digest = hashlib.sha256(
            f"{self.TICKET_PREFIX}|{company_ref}|{ticker}|{start}|{end}|{governance.content_hash}".encode("utf-8")
        ).hexdigest()[:24]
        return self.spawn(
            digest=digest,
            record={
                "company_ref": company_ref, "ticker": ticker,
                "requested_start": start, "requested_end": end,
                "governance_configured": self.configured,
                "governance_ref": governance.id,
                "governance_hash": governance.content_hash,
            },
            company_ref=company_ref, ticker=ticker, start=start, end=end,
        )


__all__ = ["TICKET_PREFIX", "MarketPriceLauncher"]
