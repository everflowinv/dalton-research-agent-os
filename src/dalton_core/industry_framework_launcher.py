"""P12e: launch the industry-framework child, one at a time.

The run is named by the state of the evidence it is about to reason over -- the
digest of the signature the coordinator computed -- so a tick firing while the
same work is still running is the *same* ticket rather than a second child
paying for the same judgement.  A Claim landing, or a quarter being filed,
changes the signature and therefore the ticket, which is what makes the lane
make progress instead of re-asking a settled question.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .lane_child_launcher import LaneChildLauncher, LaneChildRejected

TICKET_PREFIX = "industry-framework-run"


def run_digest(industry_ref: str | None, signature: str) -> str:
    payload = "|".join([TICKET_PREFIX, industry_ref or "-", signature])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


class IndustryFrameworkLauncher(LaneChildLauncher):
    """Spawn ``dalton_core.industry_framework_cli`` against the writer's state."""

    TICKET_PREFIX = TICKET_PREFIX
    TICKETS_DIRNAME = "industry-framework-runs"
    CHILD_MODULE = "dalton_core.industry_framework_cli"

    def __init__(
        self,
        *,
        state_dir: str | Path,
        model_config_path: str | Path | None = None,
        verifier_model_config_path: str | Path | None = None,
        scheduler_db: str | Path | None = None,
        policy_path: str | Path | None = None,
        max_units: int | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(state_dir=state_dir, **kwargs)
        self.model_config_path = (
            None if model_config_path is None
            else Path(model_config_path).expanduser().resolve())
        self.verifier_model_config_path = (
            None if verifier_model_config_path is None
            else Path(verifier_model_config_path).expanduser().resolve())
        self.scheduler_db = (
            None if scheduler_db is None
            else Path(scheduler_db).expanduser().resolve())
        self.policy_path = (
            None if policy_path is None
            else Path(policy_path).expanduser().resolve())
        self.max_units = max_units

    @property
    def configured(self) -> bool:
        """Whether a model is wired.

        Unlike the dossier's, this lane is *not* useless without one -- the
        cross-company table is computed and needs no judgement -- but it cannot
        publish a version without prose, so the child reports ``gated`` and
        writes nothing rather than pretending to have drafted.  The table it
        computed on the way is in the summary either way, which is how an
        operator sees that the arithmetic works before paying for a model.
        """

        return self.model_config_path is not None

    def _command(self, *, ticket_dir: Path, company_ref: str | None = None) -> list[str]:
        command = [
            self.python_executable, "-m", self.CHILD_MODULE,
            "--state-dir", str(self.state_dir),
            "--summary-dir", str(ticket_dir), "--quiet",
        ]
        if self.model_config_path is not None:
            command += ["--model-config", str(self.model_config_path)]
        if self.verifier_model_config_path is not None:
            command += ["--verifier-model-config", str(self.verifier_model_config_path)]
        if self.scheduler_db is not None:
            command += ["--scheduler-db", str(self.scheduler_db)]
        if self.policy_path is not None:
            command += ["--framework-policy", str(self.policy_path)]
        if self.max_units is not None:
            command += ["--max-units", str(self.max_units)]
        return command

    def start(self, *, signature: str, industry_ref: str | None = None) -> dict[str, Any]:
        if not isinstance(signature, str) or not signature.strip():
            raise LaneChildRejected("a framework run needs an evidence signature")
        digest = run_digest(industry_ref, signature.strip())
        return self.spawn(
            digest=digest,
            record={
                "industry_ref": industry_ref,
                "signature": signature.strip(),
                "run_digest": digest,
                "model_configured": self.configured,
            },
        )


__all__ = ["TICKET_PREFIX", "IndustryFrameworkLauncher", "run_digest"]
