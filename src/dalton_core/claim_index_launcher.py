"""P12b: launch the Claim-index child, one at a time.

The child files a bounded batch of Claims into the dossier's sections; this
decides when one runs and keeps the ticket.

The run is named by the company *and the batch it is about to reason over* --
the digest of the pending claim versions -- so a tick that fires while the same
batch is still being tagged is the same ticket rather than a second child
paying for the same judgement.  A claim tagged in between changes the batch and
therefore changes the ticket, which is what makes the lane make progress
instead of re-asking about work that is already done.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Sequence

from .lane_child_launcher import LaneChildLauncher, LaneChildRejected

TICKET_PREFIX = "claim-index-run"


def batch_digest(company_ref: str, claim_version_refs: Sequence[str]) -> str:
    """The 24-hex name of one indexing run.

    Order-independent: the same pending set found in a different order is the
    same batch, because the set is what the run is about.
    """

    payload = "|".join([TICKET_PREFIX, company_ref, *sorted(claim_version_refs)])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


class ClaimIndexLauncher(LaneChildLauncher):
    """Spawn ``dalton_core.claim_index_cli`` against the writer's state."""

    TICKET_PREFIX = TICKET_PREFIX
    TICKETS_DIRNAME = "claim-index-runs"
    CHILD_MODULE = "dalton_core.claim_index_cli"

    def __init__(
        self,
        *,
        state_dir: str | Path,
        model_config_path: str | Path | None = None,
        scheduler_db: str | Path | None = None,
        max_claims: int | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(state_dir=state_dir, **kwargs)
        self.model_config_path = (
            None if model_config_path is None
            else Path(model_config_path).expanduser().resolve()
        )
        self.scheduler_db = (
            None if scheduler_db is None
            else Path(scheduler_db).expanduser().resolve()
        )
        self.max_claims = max_claims

    @property
    def configured(self) -> bool:
        """Whether a model is wired.

        Unlike the other model lanes this one is still useful without one: the
        rules decide importance, as_of and dedupe for every claim and the
        aspect for every number, and only the aspect of qualitative prose waits
        for a model.  Without a model the child reports ``gated`` *after* it
        has written everything the rules settled.
        """

        return self.model_config_path is not None

    def _command(self, *, ticket_dir: Path, company_ref: str) -> list[str]:
        command = [
            self.python_executable, "-m", self.CHILD_MODULE,
            "--state-dir", str(self.state_dir),
            "--company-ref", company_ref,
            "--summary-dir", str(ticket_dir), "--quiet",
        ]
        if self.model_config_path is not None:
            command += ["--model-config", str(self.model_config_path)]
        if self.scheduler_db is not None:
            command += ["--scheduler-db", str(self.scheduler_db)]
        if self.max_claims is not None:
            command += ["--max-claims", str(self.max_claims)]
        return command

    def start(
        self, *, company_ref: str, claim_version_refs: Sequence[str]
    ) -> dict[str, Any]:
        if not isinstance(company_ref, str) or not company_ref.strip():
            raise LaneChildRejected("a claim index run needs a company")
        if not claim_version_refs:
            raise LaneChildRejected("a claim index run needs claims to tag")
        company_ref = company_ref.strip()
        digest = batch_digest(company_ref, claim_version_refs)
        return self.spawn(
            digest=digest,
            record={
                "company_ref": company_ref,
                "batch_digest": digest,
                "pending": len(claim_version_refs),
                "model_configured": self.configured,
            },
            company_ref=company_ref,
        )


__all__ = ["TICKET_PREFIX", "ClaimIndexLauncher", "batch_digest"]
