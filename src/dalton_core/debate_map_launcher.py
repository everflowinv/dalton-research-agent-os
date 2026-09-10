"""P12c: launch the debate-map child, one at a time.

The run is named by the subject *and the evidence it is about to reason over*
-- the fingerprint of that subject's canonical claim versions -- so a tick that
fires while the same evidence is still being argued about is the same ticket
rather than a second child paying for the same judgement.  A Claim committed in
between changes the fingerprint and therefore the ticket, which is what makes
the lane make progress instead of re-asking a question it has already answered.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .lane_child_launcher import LaneChildLauncher, LaneChildRejected

TICKET_PREFIX = "debate-map-run"


def run_digest(
    subject_ref: str, fingerprint: str, contract_fingerprint: str | None = None,
) -> str:
    """The 24-hex name of one debate-map run."""

    parts = [TICKET_PREFIX, subject_ref, fingerprint]
    if contract_fingerprint is not None:
        parts.append(contract_fingerprint)
    payload = "|".join(parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


class DebateMapLauncher(LaneChildLauncher):
    """Spawn ``dalton_core.debate_map_cli`` against the writer's state."""

    TICKET_PREFIX = TICKET_PREFIX
    TICKETS_DIRNAME = "debate-map-runs"
    CHILD_MODULE = "dalton_core.debate_map_cli"

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

    @property
    def configured(self) -> bool:
        """A debate map is judgement end to end; without a model there is none.

        Unlike the Claim index, whose rules settle most of a tag on their own,
        nothing deterministic here produces a debate.  The pre-pass finds
        *candidates*; turning one into a question with two sides is the model's
        whole job.  So a child without a model writes nothing and says
        ``gated``.
        """

        return self.model_config_path is not None

    def _command(self, *, ticket_dir: Path, subject_ref: str) -> list[str]:
        command = [
            self.python_executable, "-m", self.CHILD_MODULE,
            "--state-dir", str(self.state_dir),
            "--subject-ref", subject_ref,
            "--summary-dir", str(ticket_dir), "--quiet",
        ]
        if self.model_config_path is not None:
            command += ["--model-config", str(self.model_config_path)]
        if self.scheduler_db is not None:
            command += ["--scheduler-db", str(self.scheduler_db)]
        return command

    def start(self, *, subject_ref: str, fingerprint: str) -> dict[str, Any]:
        if not isinstance(subject_ref, str) or not subject_ref.strip():
            raise LaneChildRejected("a debate map run needs a subject")
        if not isinstance(fingerprint, str) or not fingerprint.strip():
            raise LaneChildRejected("a debate map run needs an evidence fingerprint")
        subject_ref = subject_ref.strip()
        fingerprint = fingerprint.strip()
        from .cockpit_model import verifier_provider_contract_fingerprint
        contract_fingerprint = verifier_provider_contract_fingerprint(
            "debate_map_verifier")
        digest = run_digest(subject_ref, fingerprint, contract_fingerprint)
        return self.spawn(
            digest=digest,
            record={
                "subject_ref": subject_ref,
                "evidence_fingerprint": fingerprint,
                "run_digest": digest,
                "verifier_provider_contract": contract_fingerprint,
                "model_configured": self.configured,
            },
            subject_ref=subject_ref,
        )


__all__ = ["TICKET_PREFIX", "DebateMapLauncher", "run_digest"]
