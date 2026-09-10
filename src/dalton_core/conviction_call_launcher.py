"""P15d: launch the conviction-call child, one at a time.

The run is named by the company *and the evidence it is about to reason over*
-- the fingerprint of the theses, debates, consensus metrics and calendar rows
the gate admitted it on.  A tick that fires while the same evidence is still
being argued about is the same ticket rather than a second child paying twice
for the same opinion; a new Claim or a moved debate changes the fingerprint and
therefore the ticket, which is what makes the lane make progress instead of
re-asking a question it has already answered.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .lane_child_launcher import LaneChildLauncher, LaneChildRejected

TICKET_PREFIX = "conviction-call-run"


def run_digest(
    company_ref: str, fingerprint: str, contract_fingerprint: str | None = None,
) -> str:
    """The 24-hex name of one conviction-call run."""

    parts = [TICKET_PREFIX, company_ref, fingerprint]
    if contract_fingerprint is not None:
        parts.append(contract_fingerprint)
    payload = "|".join(parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


class ConvictionCallLauncher(LaneChildLauncher):
    """Spawn ``dalton_core.conviction_call_cli`` against the writer's state."""

    TICKET_PREFIX = TICKET_PREFIX
    TICKETS_DIRNAME = "conviction-call-runs"
    CHILD_MODULE = "dalton_core.conviction_call_cli"

    def __init__(
        self,
        *,
        state_dir: str | Path,
        model_config_path: str | Path | None = None,
        scheduler_db: str | Path | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(state_dir=state_dir, **kwargs)
        self.model_config_path = (
            None if model_config_path is None
            else Path(model_config_path).expanduser().resolve()
        )
        self.scheduler_db = (
            None if scheduler_db is None else Path(scheduler_db).expanduser().resolve()
        )

    def configuration_fingerprint(self) -> str | None:
        from .model_route_recovery import configuration_fingerprint
        return configuration_fingerprint(self.model_config_path)

    @property
    def configured(self) -> bool:
        """A call is an argument; without a model there is nothing to write.

        The gate in front of it is deterministic and useful on its own -- it
        can say which companies are eligible today and why the rest are not --
        but the four sentences that make a call are judgement, so a child
        without a model reports ``gated`` and writes nothing.
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
        return command

    def start(self, *, company_ref: str, fingerprint: str) -> dict[str, Any]:
        if not isinstance(company_ref, str) or not company_ref.strip():
            raise LaneChildRejected("a conviction call run needs a company")
        if not isinstance(fingerprint, str) or not fingerprint.strip():
            raise LaneChildRejected("a conviction call run needs an evidence fingerprint")
        company_ref = company_ref.strip()
        fingerprint = fingerprint.strip()
        from .cockpit_model import verifier_provider_contract_fingerprint
        contract_fingerprint = verifier_provider_contract_fingerprint(
            "conviction_call_verifier")
        config_fingerprint = self.configuration_fingerprint()
        ticket_contract = contract_fingerprint
        if config_fingerprint is not None:
            ticket_contract += ":model-config:" + config_fingerprint
        digest = run_digest(company_ref, fingerprint, ticket_contract)
        return self.spawn(
            digest=digest,
            record={
                "company_ref": company_ref,
                "evidence_fingerprint": fingerprint,
                "run_digest": digest,
                "verifier_provider_contract": contract_fingerprint,
                "model_configured": self.configured,
                "model_config_fingerprint": config_fingerprint,
            },
            company_ref=company_ref,
        )


__all__ = ["TICKET_PREFIX", "ConvictionCallLauncher", "run_digest"]
