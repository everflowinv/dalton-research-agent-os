"""P12a: launch the dossier child, one at a time.

The run is named by the company and by the state of the evidence it is about
to reason over -- the digest of the ledger signature the coordinator computed
-- so a tick firing while the same work is still running is the *same* ticket
rather than a second child paying for the same judgement. A Claim landing in
between changes the signature and therefore the ticket, which is what makes
the lane make progress instead of re-asking a settled question.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .lane_child_launcher import LaneChildLauncher, LaneChildRejected

TICKET_PREFIX = "company-dossier-run"


def run_digest(company_ref: str | None, signature: str) -> str:
    payload = "|".join([TICKET_PREFIX, company_ref or "-", signature])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


class CompanyDossierLauncher(LaneChildLauncher):
    """Spawn ``dalton_core.company_dossier_cli`` against the writer's state."""

    TICKET_PREFIX = TICKET_PREFIX
    TICKETS_DIRNAME = "company-dossier-runs"
    CHILD_MODULE = "dalton_core.company_dossier_cli"

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

        Unlike the Claim index, this lane is useless without one: every part of
        a dossier is written prose. Without a model the child plans and reports
        ``gated`` rather than pretending to have drafted.
        """

        return self.model_config_path is not None

    def capacity_probe_interval_seconds(self) -> int | None:
        """Configured broker-capacity cooldown, or the lane's legacy default."""

        if self.model_config_path is None:
            return None
        from .document_extraction import validate_model_config
        config = validate_model_config(json.loads(
            self.model_config_path.read_text(encoding="utf-8")))
        retry = config.get("capacity_retry")
        return None if retry is None else int(retry["cooldown_seconds"])

    def _command(self, *, ticket_dir: Path, company_ref: str | None = None) -> list[str]:
        command = [
            self.python_executable, "-m", self.CHILD_MODULE,
            "--state-dir", str(self.state_dir),
            "--summary-dir", str(ticket_dir), "--quiet",
        ]
        if company_ref:
            command += ["--company-ref", company_ref]
        if self.model_config_path is not None:
            command += ["--model-config", str(self.model_config_path)]
        if self.verifier_model_config_path is not None:
            command += ["--verifier-model-config",
                        str(self.verifier_model_config_path)]
        if self.scheduler_db is not None:
            command += ["--scheduler-db", str(self.scheduler_db)]
        if self.policy_path is not None:
            command += ["--dossier-policy", str(self.policy_path)]
        if self.max_units is not None:
            command += ["--max-units", str(self.max_units)]
        return command

    def start(self, *, signature: str, company_ref: str | None = None) -> dict[str, Any]:
        if not isinstance(signature, str) or not signature.strip():
            raise LaneChildRejected("a dossier run needs a ledger signature")
        digest = run_digest(company_ref, signature.strip())
        return self.spawn(
            digest=digest,
            record={
                "company_ref": company_ref,
                "signature": signature.strip(),
                "run_digest": digest,
                "model_configured": self.configured,
            },
            company_ref=company_ref,
        )


__all__ = ["TICKET_PREFIX", "CompanyDossierLauncher", "run_digest"]
