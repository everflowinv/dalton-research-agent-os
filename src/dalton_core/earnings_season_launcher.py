"""P14f: launch the earnings-season child, one at a time.

Named by the mission version, the occurrence and the window, so a tick that
fires while the same window is still the most urgent thing names the same
ticket rather than a second child paying to write the same preview.  (The
deliverable's idempotency key would refuse the second document anyway; this is
what stops it being paid for first.)
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .lane_child_launcher import LaneChildLauncher, LaneChildRejected

TICKET_PREFIX = "earnings-season-run"


class EarningsSeasonLauncher(LaneChildLauncher):
    """Spawn ``dalton_core.earnings_season_cli`` against the writer's state."""

    TICKET_PREFIX = TICKET_PREFIX
    TICKETS_DIRNAME = "earnings-season-runs"
    CHILD_MODULE = "dalton_core.earnings_season_cli"

    def __init__(
        self,
        *,
        state_dir: str | Path,
        writer_model_config: str | Path,
        verifier_model_config: str | Path,
        policy_path: str | Path | None = None,
        scheduler_db: str | Path | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(state_dir=state_dir, **kwargs)
        self.writer_model_config = Path(writer_model_config).expanduser().resolve()
        self.verifier_model_config = Path(verifier_model_config).expanduser().resolve()
        self.policy_path = None if policy_path is None else Path(policy_path).expanduser().resolve()
        self.scheduler_db = None if scheduler_db is None else Path(scheduler_db)

    @property
    def configured(self) -> bool:
        """Both configurations, or nothing.

        A calibration proposes a thesis revision to a person.  One written
        without an independent check would look verified in the record because
        the field exists, and would not be.
        """

        return self.writer_model_config.is_file() and self.verifier_model_config.is_file()

    def _command(self, *, ticket_dir: Path) -> list[str]:
        command = [
            self.python_executable, "-m", self.CHILD_MODULE,
            "--state-dir", str(self.state_dir),
            "--model-config", str(self.writer_model_config),
            "--verifier-model-config", str(self.verifier_model_config),
            "--summary-dir", str(ticket_dir), "--quiet",
        ]
        if self.policy_path is not None:
            command += ["--tracking-policy", str(self.policy_path)]
        if self.scheduler_db is not None:
            command += ["--scheduler", str(self.scheduler_db)]
        return command

    def start(self, *, batch_ref: str) -> dict[str, Any]:
        if not isinstance(batch_ref, str) or not batch_ref.strip():
            raise LaneChildRejected("an earnings-season run needs a batch ref")
        if not self.configured:
            raise LaneChildRejected(
                "the earnings-season lane needs a writer and a verifier configuration"
            )
        from .cockpit_model import verifier_provider_contract_fingerprint
        provider_contract = verifier_provider_contract_fingerprint(
            "earnings_preview_verifier", "earnings_calibration_verifier")
        digest = hashlib.sha256(
            f"{self.TICKET_PREFIX}|{batch_ref.strip()}|{provider_contract}".encode("utf-8")
        ).hexdigest()[:24]
        return self.spawn(digest=digest, record={"batch_ref": batch_ref.strip()})


__all__ = ["TICKET_PREFIX", "EarningsSeasonLauncher"]
