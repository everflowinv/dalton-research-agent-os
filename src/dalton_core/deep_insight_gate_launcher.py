"""P12d: launch the gate child, one at a time.

The run is named by the company and by the state of the evidence it is about to
reason over -- the digest of the ledger signature the coordinator computed -- so
a tick firing while the same work is still running is the *same* ticket rather
than a second child paying for the same twelve answers.  A new dossier version
in between changes the signature and therefore the ticket, which is what makes
the lane make progress instead of re-asking a settled question.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .lane_child_launcher import LaneChildLauncher, LaneChildRejected

TICKET_PREFIX = "deep-insight-gate-run"


def run_digest(company_ref: str | None, signature: str) -> str:
    payload = "|".join([TICKET_PREFIX, company_ref or "-", signature])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


class DeepInsightGateLauncher(LaneChildLauncher):
    """Spawn ``dalton_core.deep_insight_gate_cli`` against the writer's state."""

    TICKET_PREFIX = TICKET_PREFIX
    TICKETS_DIRNAME = "deep-insight-gate-runs"
    CHILD_MODULE = "dalton_core.deep_insight_gate_cli"

    def __init__(
        self,
        *,
        state_dir: str | Path,
        model_config_path: str | Path | None = None,
        verifier_model_config_path: str | Path | None = None,
        scheduler_db: str | Path | None = None,
        policy_path: str | Path | None = None,
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
        self._adopted_finished: set[str] = set()

    @property
    def configured(self) -> bool:
        """Whether a model is wired.

        The gate is useless without one: every one of the twelve answers is
        written prose.  Without a model the child plans and reports ``gated``
        rather than pretending to have answered.
        """

        return self.model_config_path is not None

    def _command(self, *, ticket_dir: Path, company_ref: str | None = None,
                 source_fingerprint: str | None = None) -> list[str]:
        command = [
            self.python_executable, "-m", self.CHILD_MODULE,
            "--state-dir", str(self.state_dir),
            "--summary-dir", str(ticket_dir), "--quiet",
        ]
        if company_ref:
            command += ["--company-ref", company_ref]
        if source_fingerprint:
            command += ["--source-fingerprint", source_fingerprint]
        if self.model_config_path is not None:
            command += ["--model-config", str(self.model_config_path)]
        if self.verifier_model_config_path is not None:
            command += ["--verifier-model-config",
                        str(self.verifier_model_config_path)]
        if self.scheduler_db is not None:
            command += ["--scheduler-db", str(self.scheduler_db)]
        if self.policy_path is not None:
            command += ["--gate-policy", str(self.policy_path)]
        return command

    def start(self, *, signature: str, company_ref: str | None = None,
              source_fingerprint: str | None = None) -> dict[str, Any]:
        if not isinstance(signature, str) or not signature.strip():
            raise LaneChildRejected("a gate run needs a ledger signature")
        digest = run_digest(company_ref, signature.strip())
        if (company_ref is not None and
                (not isinstance(source_fingerprint, str)
                 or len(source_fingerprint) != 64)):
            raise LaneChildRejected("a company gate run needs its sha256 source fingerprint")
        ticket_id = f"{self.TICKET_PREFIX}:{digest}"
        if (self._current is None and ticket_id not in self._adopted_finished
                and self._ticket_path(ticket_id).is_file()):
            adopted = self.status(ticket_id)
            if adopted.get("status") != "running":
                self._adopted_finished.add(ticket_id)
            return adopted
        return self.spawn(
            digest=digest,
            record={
                "company_ref": company_ref,
                "source_fingerprint": source_fingerprint,
                "signature": signature.strip(),
                "run_digest": digest,
                "model_configured": self.configured,
            },
            company_ref=company_ref, source_fingerprint=source_fingerprint,
        )


__all__ = ["TICKET_PREFIX", "DeepInsightGateLauncher", "run_digest"]
