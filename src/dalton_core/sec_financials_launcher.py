"""P13aj: launch the SEC statements child, one company at a time.

The child parses a filing and validates an observation; this decides when one
runs and keeps the ticket. It refuses before spawning when the governance
record on disk is not approved, because a launch that was always going to be
refused still costs a process and a ticket, and the reason arrives further from
the cause.

The run is named by what it asks for -- company, form, how many filings -- so
asking twice for the same thing is the same ticket rather than a second child
doing identical work.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable, Sequence

from .lane_child_launcher import (
    LaneChildLauncher,
    LaneChildRejected,
)

TICKET_PREFIX = "sec-financials-run"
LIVE_MODE_ARGS: tuple[str, ...] = ("--allow-network",)
MAX_FILINGS = 8


class SecFinancialsLauncher(LaneChildLauncher):
    """Spawn ``dalton_core.sec_financials_cli`` against the writer's state."""

    TICKET_PREFIX = TICKET_PREFIX
    TICKETS_DIRNAME = "sec-financials-runs"
    CHILD_MODULE = "dalton_core.sec_financials_cli"

    def __init__(
        self,
        *,
        state_dir: str | Path,
        governance_path: str | Path,
        mode_args: Sequence[str] = LIVE_MODE_ARGS,
        user_agent: str | None = None,
        scheduling_forms: Sequence[str] = ("10-K", "10-Q"),
        scheduling_limits: dict[str, int] | None = None,
        governance_loader: Callable[[Path], Any] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(state_dir=state_dir, **kwargs)
        self.governance_path = Path(governance_path).expanduser().resolve()
        self.mode_args = tuple(str(item) for item in mode_args)
        self.user_agent = user_agent
        self.scheduling_forms = tuple(scheduling_forms)
        self.scheduling_limits = dict(
            {"10-K": 1} if scheduling_limits is None else scheduling_limits
        )
        self._governance_loader = governance_loader

    @property
    def networked(self) -> bool:
        return "--allow-network" in self.mode_args

    def load_governance(self) -> Any:
        from .connector_governance import ConnectorGovernance

        loader = self._governance_loader or ConnectorGovernance.load
        return loader(self.governance_path)

    def _command(self, *, ticket_dir: Path, ticker: str, form: str,
                 limit: int) -> list[str]:
        command = [
            self.python_executable, "-m", self.CHILD_MODULE,
            "--state-dir", str(self.state_dir),
            "--governance", str(self.governance_path),
            "--ticker", ticker, "--form", form, "--limit", str(limit),
            "--summary-dir", str(ticket_dir), "--quiet",
        ]
        if self.user_agent:
            command += ["--user-agent", self.user_agent]
        return command + list(self.mode_args)

    def start(self, *, ticker: str, form: str = "10-Q", limit: int = 1,
              actor_ref: str) -> dict[str, Any]:
        if not isinstance(ticker, str) or not ticker.strip():
            raise LaneChildRejected("a statements run needs a ticker")
        if form not in ("10-Q", "10-K"):
            raise LaneChildRejected("form must be 10-Q or 10-K")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_FILINGS:
            raise LaneChildRejected(f"limit must be 1..{MAX_FILINGS}")
        if not isinstance(actor_ref, str) or not actor_ref.startswith(
            ("human:", "automation:")
        ):
            raise LaneChildRejected("actor must use the human: or automation: namespace")
        # Refuse here rather than after a process exists: the child would reach
        # the same conclusion, having cost a spawn and a ticket to say so.
        governance = self.load_governance()
        if not getattr(governance, "approved", False):
            raise LaneChildRejected(
                "SEC financial-statements governance record is not approved"
            )
        ticker = ticker.strip().upper()
        digest = hashlib.sha256(
            f"{self.TICKET_PREFIX}|{ticker}|{form}|{limit}|{governance.content_hash}"
            .encode("utf-8")
        ).hexdigest()[:24]
        return self.spawn(
            digest=digest,
            record={
                "ticker": ticker, "form": form, "limit": limit,
                "actor_ref": actor_ref,
                "governance_ref": governance.id,
                "governance_hash": governance.content_hash,
                "transport": "public-https" if self.networked else "fixture",
            },
            ticker=ticker, form=form, limit=limit,
        )


__all__ = ["LIVE_MODE_ARGS", "MAX_FILINGS", "TICKET_PREFIX", "SecFinancialsLauncher"]
