"""W4: launch the zero-base review child, one at a time.

The ticket is named by the mode and by what the run is *about*: the set of
companies that are due, or the digest of the outcome pass.  A tick that fires
while nothing has moved therefore names the same ticket rather than paying to
re-read the same table, and a company that becomes due half an hour later is
a different ticket rather than a collision with the run before it.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Sequence

from .lane_child_launcher import LaneChildLauncher, LaneChildRejected

TICKET_PREFIX = "zero-base-review-run"


class ZeroBaseReviewLauncher(LaneChildLauncher):
    """Spawn ``dalton_core.zero_base_review_cli`` against the writer's state."""

    TICKET_PREFIX = TICKET_PREFIX
    TICKETS_DIRNAME = "zero-base-review-runs"
    CHILD_MODULE = "dalton_core.zero_base_review_cli"

    def __init__(
        self,
        *,
        state_dir: str | Path,
        model_config: str | Path | None = None,
        policy_path: str | Path | None = None,
        scheduler_db: str | Path | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(state_dir=state_dir, **kwargs)
        self.model_config = (
            None if model_config is None else Path(model_config).expanduser().resolve()
        )
        self.policy_path = (
            None if policy_path is None else Path(policy_path).expanduser().resolve()
        )
        self.scheduler_db = None if scheduler_db is None else Path(scheduler_db)

    @property
    def configured(self) -> bool:
        """Whether a *review* can run here.

        The check pass has no model in it and runs on any Core; a review
        without a model configuration cannot, and saying so here is what keeps
        the lane from launching a child whose only possible answer is
        ``gated``.
        """

        return self.model_config is not None and self.model_config.is_file()

    def _command(self, *, ticket_dir: Path, mode: str = "review", **_: Any) -> list[str]:
        command = [
            self.python_executable, "-m", self.CHILD_MODULE,
            "--state-dir", str(self.state_dir),
            "--summary-dir", str(ticket_dir),
            "--mode", mode, "--quiet",
        ]
        if self.model_config is not None:
            command += ["--model-config", str(self.model_config)]
        if self.policy_path is not None:
            command += ["--tracking-policy", str(self.policy_path)]
        if self.scheduler_db is not None:
            command += ["--scheduler", str(self.scheduler_db)]
        return command

    def start(
        self, *, mode: str, batch_ref: str, company_refs: Sequence[str] = ()
    ) -> dict[str, Any]:
        if mode not in ("review", "checks"):
            raise LaneChildRejected("a zero-base run is a review or a check pass")
        if not isinstance(batch_ref, str) or not batch_ref.strip():
            raise LaneChildRejected("a zero-base run needs a batch ref")
        if mode == "review" and not self.configured:
            raise LaneChildRejected(
                "the zero-base review lane needs a model configuration to review"
            )
        digest = hashlib.sha256(
            f"{self.TICKET_PREFIX}|{mode}|{batch_ref.strip()}".encode("utf-8")
        ).hexdigest()[:24]
        return self.spawn(
            digest=digest,
            record={"mode": mode, "batch_ref": batch_ref.strip(),
                    "company_refs": list(company_refs)},
            mode=mode,
        )


__all__ = ["TICKET_PREFIX", "ZeroBaseReviewLauncher"]
