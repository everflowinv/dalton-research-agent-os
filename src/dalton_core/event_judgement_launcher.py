"""P14a: launch the judgement child, one at a time.

Named by the mission version and the newest unjudged event: a tick that fires
while nothing new has arrived is the same ticket rather than a second child
paying to re-read a batch it already decided.  (The ledger would refuse the
second judgement anyway; this is what stops it being paid for first.)
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .lane_child_launcher import LaneChildLauncher, LaneChildRejected

TICKET_PREFIX = "event-judgement-run"


class EventJudgementLauncher(LaneChildLauncher):
    """Spawn ``dalton_core.event_judgement_cli`` against the writer's state."""

    TICKET_PREFIX = TICKET_PREFIX
    TICKETS_DIRNAME = "event-judgement-runs"
    CHILD_MODULE = "dalton_core.event_judgement_cli"

    def __init__(
        self,
        *,
        state_dir: str | Path,
        judge_model_config: str | Path,
        verifier_model_config: str | Path,
        policy_path: str | Path | None = None,
        scheduler_db: str | Path | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(state_dir=state_dir, **kwargs)
        self.judge_model_config = Path(judge_model_config).expanduser().resolve()
        self.verifier_model_config = Path(verifier_model_config).expanduser().resolve()
        self.policy_path = None if policy_path is None else Path(policy_path).expanduser().resolve()
        self.scheduler_db = None if scheduler_db is None else Path(scheduler_db)

    @property
    def configured(self) -> bool:
        """Both configurations, or nothing.

        A judge with no independent verifier is the one shape this lane must
        never take: it would produce decisions that look verified in the
        record because the field exists, and are not.
        """

        return self.judge_model_config.is_file() and self.verifier_model_config.is_file()

    def configuration_signature(self) -> str:
        """Changing a Cockpit route makes an unjudged batch retryable."""
        from .cockpit_model import verifier_provider_contract_fingerprint

        digest = hashlib.sha256(b"event-configuration:1")
        for path in (self.judge_model_config, self.verifier_model_config, self.policy_path):
            if path is None:
                digest.update(b"\0absent")
            else:
                digest.update(b"\0file:")
                digest.update(hashlib.sha256(path.read_bytes()).digest())
        digest.update(b"\0provider-contracts:")
        digest.update(verifier_provider_contract_fingerprint(
            "event_judgement_verifier", "thesis_reflection_verifier"
        ).encode("ascii"))
        return digest.hexdigest()

    def _command(self, *, ticket_dir: Path, company_ref: str,
                 event_ref: str) -> list[str]:
        command = [
            self.python_executable, "-m", self.CHILD_MODULE,
            "--state-dir", str(self.state_dir),
            "--judge-model-config", str(self.judge_model_config),
            "--verifier-model-config", str(self.verifier_model_config),
            "--company-ref", company_ref, "--event-ref", event_ref,
            "--summary-dir", str(ticket_dir), "--quiet",
        ]
        if self.policy_path is not None:
            command += ["--tracking-policy", str(self.policy_path)]
        if self.scheduler_db is not None:
            command += ["--scheduler", str(self.scheduler_db)]
        return command

    def start(self, *, batch_ref: str, company_ref: str,
              event_ref: str, group_key: str) -> dict[str, Any]:
        if not isinstance(batch_ref, str) or not batch_ref.strip():
            raise LaneChildRejected("a judgement run needs a batch ref")
        if not self.configured:
            raise LaneChildRejected(
                "the judgement lane needs a judge and a verifier configuration"
            )
        if not all(isinstance(value, str) and value.strip()
                   for value in (company_ref, event_ref, group_key)):
            raise LaneChildRejected("a judgement run needs a company and event group")
        digest = hashlib.sha256(
            f"{self.TICKET_PREFIX}|{batch_ref.strip()}".encode("utf-8")
        ).hexdigest()[:24]
        return self.spawn(
            digest=digest,
            record={"batch_ref": batch_ref.strip(), "company_ref": company_ref,
                    "event_ref": event_ref, "group_key": group_key},
            company_ref=company_ref, event_ref=event_ref,
        )


__all__ = ["TICKET_PREFIX", "EventJudgementLauncher"]
