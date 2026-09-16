"""C2-4: the child ticket for the deterministic quantitative promoter.

Same shape as every other lane child: one slot, owner-only tickets under
``<state>/quantitative-claim-promotion-runs/<ticket>/``, a ``summary.json`` the
coordinator reads back.  No model configuration, no broker, no budget: this
lane arithmetically cannot spend money, which is why it has no failure budget
and no permission control either.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Sequence

from .lane_child_launcher import LaneChildLauncher

TICKET_PREFIX = "quantitative-claim-promotion-run"
DEFAULT_LIMIT = 200


def batch_digest(company_ref: str | None, limit: int) -> str:
    payload = "|".join([TICKET_PREFIX, company_ref or "*", str(limit)])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


class QuantitativeClaimPromotionLauncher(LaneChildLauncher):
    TICKET_PREFIX = TICKET_PREFIX
    TICKETS_DIRNAME = "quantitative-claim-promotion-runs"
    CHILD_MODULE = "dalton_core.quantitative_claim_promotion_cli"

    def __init__(
        self,
        *,
        state_dir: str | Path,
        candidate_staging: str | Path | None = None,
        limit: int = DEFAULT_LIMIT,
        **kwargs: Any,
    ) -> None:
        super().__init__(state_dir=state_dir, **kwargs)
        self.candidate_staging = (
            None if candidate_staging is None
            else Path(candidate_staging).expanduser().resolve())
        self.limit = int(limit)

    @property
    def configured(self) -> bool:
        return True

    def _command(self, *, ticket_dir: Path, company_ref: str | None = None,
                 limit: int | None = None) -> list[str]:
        command = [
            self.python_executable, "-m", self.CHILD_MODULE,
            "--state-dir", str(self.state_dir),
            "--summary-dir", str(ticket_dir),
            "--limit", str(self.limit if limit is None else limit),
            "--quiet",
        ]
        if self.candidate_staging is not None:
            command += ["--candidate-staging", str(self.candidate_staging)]
        if company_ref is not None:
            command += ["--company-ref", company_ref]
        return command

    def start(self, *, company_ref: str | None = None,
              limit: int | None = None) -> dict[str, Any]:
        bound = self.limit if limit is None else int(limit)
        if not 1 <= bound <= 5000:
            raise ValueError("limit must be 1..5000")
        return self.spawn(
            digest=batch_digest(company_ref, bound),
            record={"company_ref": company_ref, "limit": bound},
            company_ref=company_ref, limit=bound,
        )


__all__ = [
    "DEFAULT_LIMIT",
    "QuantitativeClaimPromotionLauncher",
    "TICKET_PREFIX",
    "batch_digest",
]
