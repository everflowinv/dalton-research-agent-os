"""W4: launch the Hong Kong disclosure child, one at a time, named by what it reads.

Named by the operation, the stock code and the window rather than by the day.
That is the difference that matters here: the buy-back tape is one document per
*trading day* and a ticket keyed by the day the tick ran would collapse a
backfill of last week into one run and lose four days of it.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .hkex_filings_core import (
    DAILY_BUYBACK_TAPE_OPERATION,
    KIND_BY_OPERATION,
    NEXT_DAY_DISCLOSURE_OPERATION,
    OPERATIONS,
    normalise_ticker,
)
from .lane_child_launcher import LaneChildLauncher, LaneChildRejected

TICKET_PREFIX = "hkex-filings-run"
# The approved record's filename under the live state's governance directory,
# per operation. Its presence is what turns that operation on: a Core approved
# for the buy-back tape and not for Disclosure of Interests reads the first and
# reports the second as unapproved, rather than refusing to start.
GOVERNANCE_FILENAME_BY_OPERATION = {
    operation: f"{kind}-v1.json" for operation, kind in KIND_BY_OPERATION.items()
}


class HkexFilingsLauncher(LaneChildLauncher):
    """Spawn ``dalton_core.hkex_filings_cli`` against the writer's state."""

    TICKET_PREFIX = TICKET_PREFIX
    TICKETS_DIRNAME = "hkex-filings-runs"
    CHILD_MODULE = "dalton_core.hkex_filings_cli"

    def __init__(
        self,
        *,
        state_dir: str | Path,
        governance_dir: str | Path | None = None,
        actor_ref: str = "automation:coverage-mission",
        **kwargs: Any,
    ) -> None:
        super().__init__(state_dir=state_dir, **kwargs)
        self.governance_dir = (
            None if governance_dir is None
            else Path(governance_dir).expanduser().resolve()
        )
        self.actor_ref = actor_ref

    def governance_path(self, operation: str) -> Path | None:
        """The approved record for one operation, if this Core has one."""

        if self.governance_dir is None or operation not in OPERATIONS:
            return None
        path = self.governance_dir / GOVERNANCE_FILENAME_BY_OPERATION[operation]
        return path if path.is_file() else None

    def daily_acquisition_governance_path(self) -> Path | None:
        if self.governance_dir is None:
            return None
        path = self.governance_dir / GOVERNANCE_FILENAME_BY_OPERATION[
            DAILY_BUYBACK_TAPE_OPERATION]
        return path if path.is_file() else None

    def approved_operations(self) -> tuple[str, ...]:
        """Which of the four this Core may run at all, in the frozen order."""

        return tuple(
            operation for operation in OPERATIONS
            if self.governance_path(operation) is not None
        )

    @property
    def configured(self) -> bool:
        return bool(self.approved_operations())

    def _command(
        self, *, ticket_dir: Path, operation: str, hk_ticker: str, company_ref: str,
        as_of: str | None, since: str | None, until: str | None,
        headline_category: str | None, prior_rows_file: str | None,
        current_price: str | None,
    ) -> list[str]:
        command = [
            self.python_executable, "-m", self.CHILD_MODULE,
            "--state-dir", str(self.state_dir),
            "--governance", str(self.governance_path(operation)),
            "--operation", operation,
            "--hk-ticker", hk_ticker,
            "--company-ref", company_ref,
            "--actor-ref", self.actor_ref,
            "--allow-network",
            "--summary-dir", str(ticket_dir), "--quiet",
        ]
        if (operation == NEXT_DAY_DISCLOSURE_OPERATION
                and self.daily_acquisition_governance_path() is not None):
            command.extend(["--daily-buyback-tape-governance",
                            str(self.daily_acquisition_governance_path())])
        for flag, value in (
            ("--as-of", as_of), ("--since", since), ("--until", until),
            ("--headline-category", headline_category),
            ("--prior-rows-file", prior_rows_file),
            ("--current-price", current_price),
        ):
            if value:
                command.extend([flag, str(value)])
        return command

    def start(
        self,
        *,
        operation: str,
        hk_ticker: str,
        company_ref: str,
        as_of: str | None = None,
        since: str | None = None,
        until: str | None = None,
        headline_category: str | None = None,
        prior_rows_file: str | None = None,
        current_price: str | None = None,
    ) -> dict[str, Any]:
        if operation not in OPERATIONS:
            raise LaneChildRejected(
                f"{operation!r} is not an hkex-filings operation"
            )
        if self.governance_path(operation) is None:
            raise LaneChildRejected(
                "a Hong Kong disclosure run needs an approved "
                f"{KIND_BY_OPERATION[operation]} record"
            )
        if not isinstance(company_ref, str) or not company_ref.strip():
            raise LaneChildRejected("a Hong Kong disclosure run needs a company_ref")
        ticker = normalise_ticker(hk_ticker)
        # The same check the child makes, made here too, so a missing window
        # costs nothing rather than a process.
        if operation == NEXT_DAY_DISCLOSURE_OPERATION:
            if not as_of:
                raise LaneChildRejected("the buy-back tape is read one printed day "
                                        "at a time and needs an as_of")
            window = as_of
        else:
            if not (since and until):
                raise LaneChildRejected(f"{operation} needs a since and an until")
            window = f"{since}:{until}"
        digest = hashlib.sha256(
            f"{self.TICKET_PREFIX}|{operation}|{ticker}|{window}".encode("utf-8")
        ).hexdigest()[:24]
        return self.spawn(
            digest=digest,
            record={
                "company_ref": company_ref.strip(), "operation": operation,
                "hk_ticker": ticker, "as_of": as_of, "since": since, "until": until,
                "headline_category": headline_category,
                "governance_configured": self.governance_path(operation) is not None,
            },
            operation=operation, hk_ticker=ticker, company_ref=company_ref.strip(),
            as_of=as_of, since=since, until=until,
            headline_category=headline_category, prior_rows_file=prior_rows_file,
            current_price=current_price,
        )


__all__ = [
    "GOVERNANCE_FILENAME_BY_OPERATION",
    "TICKET_PREFIX",
    "HkexFilingsLauncher",
]
